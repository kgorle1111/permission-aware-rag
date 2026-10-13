"""The platform retrieval rungs on REAL data: the EnronQA sample with header-derived reader ACLs.

Same rungs and same platform ingest + retrieve code path as rung_benchmark.py (hash baseline, fastembed
dense, SPLADE hybrid, cross-encoder rerank), but the corpus, questions and ACL oracle come from
evals/enronqa/run.py (imported, not copied). One fresh worker per rung with its own SQLite, Qdrant and
model settings; the parent scores the worker's raw top-10 lists against the oracle.

Metrics: owner hit@1/4/10 and MRR@10 (Wilson + mailbox-clustered bootstrap CIs), leaks (non-reader and
exact-content-probe attempts, Wilson upper bound), a NoFilter positive control, hidden-document invariance
on a subset, latency. Needs requirements-models.txt and pyarrow; run from platform/:

    python scripts/rung_enronqa.py --mailboxes 12 --output-dir ../evals/results/2026-10-12-rungs-on-enronqa
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
from pathlib import Path
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
ENRON = REPO_ROOT / "evals" / "enronqa"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rung_benchmark as rb  # noqa: E402

KS = (1, 4, 10)
TOP = max(KS)
RUNGS = {**rb.RUNGS, "hybrid_floor_0.1": {"EMBED_BACKEND": "fastembed", "SPARSE_BACKEND": "splade", "MIN_SCORE": "0.1"}}
DEFAULT_RUNGS = ("baseline", "dense", "dense_floor_0.1", "hybrid", "hybrid_floor_0.1", "hybrid_rerank",
                 "hybrid_rerank_no_floor")
_enron = None


def enron():
    """evals/enronqa/run.py as a module. Its sys.path edits are undone so platform imports stay unshadowed."""
    global _enron
    if _enron is None:
        saved = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location("enronqa_run", ENRON / "run.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["enronqa_run"] = mod
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = saved
        _enron = mod
    return _enron


# ---------- pure scoring (parent side; no models) ----------
def first_rank(results: list, gold_doc: str) -> int | None:
    """1-based rank of the first chunk of the gold document in `results` ([doc_id, score] pairs), else None."""
    return next((i for i, r in enumerate(results, 1) if r[0] == gold_doc), None)


def owner_metrics(ranks: list[int | None], clusters: list[str]) -> dict:
    """hit@k and MRR@TOP over per-question first ranks; Wilson for hits, cluster bootstrap for both."""
    e = enron()
    out = {"n": len(ranks)}
    for k in KS:
        hits = [int(r is not None and r <= k) for r in ranks]
        out[f"hit@{k}"] = e.prop(sum(hits), len(hits)) | {"cluster_bootstrap95": e.bootstrap_ci(hits, clusters)}
    rr = [1 / r if r is not None and r <= TOP else 0.0 for r in ranks]
    out["mrr@10"] = {"mean": sum(rr) / len(rr), "cluster_bootstrap95": e.bootstrap_ci(rr, clusters)}
    return out


def leak_metrics(attempts: list[tuple[str, list]], readers: dict[str, set[str]]) -> dict:
    """attempts = (querying user, [[doc_id, score], ...]). A leak is a returned chunk whose document the
    user cannot read per `readers`; unknown documents are unreadable (fail closed)."""
    bad_chunks = [sum(u not in readers.get(r[0], ()) for r in res) for u, res in attempts]
    leaking = sum(b > 0 for b in bad_chunks)
    p = enron().prop(leaking, len(attempts))
    return p | {"leaked_chunks": sum(bad_chunks), "upper95": p["wilson95"][1],
                "attempts_returning_anything": sum(bool(res) for _, res in attempts)}


def paired_diff(a_ranks: list, b_ranks: list, clusters: list[str], k: int | None) -> dict:
    """Mean of (a - b) per question with a mailbox-clustered bootstrap CI. k=None: MRR@TOP, else hit@k.
    Paired, because both rungs answer the same questions; unpaired CIs overstate the noise in a comparison."""
    def val(r):
        if k is None:
            return 1 / r if r is not None and r <= TOP else 0.0
        return float(r is not None and r <= k)
    d = [val(x) - val(y) for x, y in zip(a_ranks, b_ranks, strict=True)]
    lo, hi = enron().bootstrap_ci(d, clusters)
    return {"mean": sum(d) / len(d), "cluster_bootstrap95": [lo, hi]}


def same_up_to_ties(a: list, b: list) -> bool:
    """(doc, text, score) lists equal as rankings: identical score sequence, and the same chunks within every
    score group except the last (the top-k cut may split a tie group, and which tied chunk survives is the
    engine's arbitrary choice). Exact-score ties between distinct chunks are common here: mail is full of
    identical quoted paragraphs, which embed to identical vectors."""
    if [x[2] for x in a] != [x[2] for x in b]:
        return False
    ga, gb = collections.defaultdict(set), collections.defaultdict(set)
    for g, xs in ((ga, a), (gb, b)):
        for doc, text, score in xs:
            g[score].add((doc, text))
    last = a[-1][2] if a else None
    return all(ga[k] == gb[k] for k in ga if k != last)


def latency(ms: list[float]) -> dict:
    return {"n": len(ms), "p50": round(rb.percentile(ms, 0.5), 1), "p95": round(rb.percentile(ms, 0.95), 1)}


# ---------- sample construction (parent) ----------
def build_sample(a) -> dict:
    """Corpus in platform format, plus the exact list of retrieval jobs every rung must answer."""
    e = enron()
    rows_all = e.load_rows(Path(a.data))
    amap, map_stats = e.build_address_map(rows_all)
    size = collections.Counter(r["user"] for r in rows_all)
    # max_emails bounds corpus size (Qdrant local mode scans every chunk per query) while keeping many mailbox clusters
    mailboxes = e.sample_mailboxes([r for r in rows_all if size[r["user"]] <= a.max_emails], a.mailboxes,
                                   a.min_emails, e.SEED)
    mset = set(mailboxes)
    rows = [r for r in rows_all if r["user"] in mset]
    docs = e.build_acl(rows, amap)
    readers = {d: v["readers"] for d, v in docs.items()}
    doc_of_path = {p: d for d, v in docs.items() for p in v["paths"]}
    qs = e.make_questions(rows, doc_of_path, a.questions, e.SEED)
    rnd = random.Random(e.SEED + 1)
    jobs = []
    for i, q in enumerate(qs):
        jobs.append({"id": f"q{i}-owner", "kind": "owner", "qid": i, "user": q["user"], "text": q["question"],
                     "gold": q["gold_doc"]})
        non = [u for u in mailboxes if u not in readers[q["gold_doc"]]]
        for j, u in enumerate(rnd.sample(non, a.non_readers)):
            jobs.append({"id": f"q{i}-non{j}", "kind": "nonreader", "qid": i, "user": u, "text": q["question"],
                         "gold": q["gold_doc"]})
    df = collections.Counter()
    for v in docs.values():
        df.update(set(e.tokenize(e.body_of(v["text"]))))
    for i, d in enumerate(random.Random(e.SEED + 2).sample(sorted(docs), min(a.probes, len(docs)))):
        shingle = e.distinctive_shingle(docs[d]["text"], df, len(docs))
        jobs.append({"id": f"p{i}-owner", "kind": "probe_owner", "qid": i, "user": sorted(readers[d])[0],
                     "text": shingle, "gold": d})
        non = [u for u in mailboxes if u not in readers[d]]
        for j, u in enumerate(rnd.sample(non, a.non_readers)):
            jobs.append({"id": f"p{i}-non{j}", "kind": "probe_nonreader", "qid": i, "user": u, "text": shingle,
                         "gold": d})
    # invariance subset: three mailboxes, each asked 15 of its own questions and 10 other mailboxes' questions
    inv = []
    for u in random.Random(e.SEED + 3).sample(mailboxes, min(3, len(mailboxes))):
        own = [q["question"] for q in qs if q["user"] == u][:15]
        foreign = [q["question"] for q in qs if q["user"] != u][:10]
        inv.append({"user": u, "queries": own + foreign})
    corpus = [{"doc_id": d, "acl": e.acl_for(readers[d]), "sections": [{"text": v["text"]}]}
              for d, v in docs.items()]
    return {"corpus": corpus, "jobs": jobs, "invariance": inv, "readers": {d: sorted(r) for d, r in readers.items()},
            "meta": {"mailboxes": mailboxes, "n_docs": len(docs), "n_questions": len(qs),
                     "n_probes": min(a.probes, len(docs)), "non_readers_per_query": a.non_readers,
                     "docs_with_2plus_readers": sum(len(r) > 1 for r in readers.values()),
                     "address_map": map_stats, "enronqa_seed": e.SEED,
                     "question_users": [q["user"] for q in qs]}}


def score_rung(raw: dict, sample: dict) -> dict:
    """Everything the table needs from one worker's raw output."""
    readers = {d: set(r) for d, r in sample["readers"].items()}
    by_kind = collections.defaultdict(list)
    for j in sample["jobs"]:
        by_kind[j["kind"]].append((j, raw["results"][j["id"]]))
    owner = by_kind["owner"]
    ranks = [first_rank(res["top"], j["gold"]) for j, res in owner]
    out = {"config": raw["config"], "models": raw["models"], "ingest_s": raw["ingest_s"], "chunks": raw["chunks"],
           "owner": owner_metrics(ranks, [j["user"] for j, _ in owner]),
           "owner_returned_nothing": sum(not res["top"] for _, res in owner),
           "latency_owner_ms": latency([res["ms"] for _, res in owner])}
    for kind, name in (("nonreader", "nonreader_questions"), ("probe_nonreader", "nonreader_probes")):
        out[name] = leak_metrics([(j["user"], res["top"]) for j, res in by_kind[kind]], readers)
    out["owner_queries_leak_check"] = leak_metrics([(j["user"], res["top"]) for j, res in owner], readers)
    out["leaks_all_nonreader_attempts"] = leak_metrics(
        [(j["user"], res["top"]) for k in ("nonreader", "probe_nonreader") for j, res in by_kind[k]], readers)
    pr = by_kind["probe_owner"]
    out["probe_owner_hit@10"] = enron().prop(sum(first_rank(res["top"], j["gold"]) is not None for j, res in pr), len(pr))
    ctl = raw["control_nofilter"]
    out["control_nofilter"] = leak_metrics([(j["user"], ctl[j["id"]]) for j in sample["jobs"] if j["id"] in ctl], readers)
    out["invariance"] = raw["invariance"]
    return out


# ---------- worker (child process; platform code path) ----------
def _worker(rung: str, sample_path: str, out_path: str, master_cache: str, phase: str) -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from app import config, embeddings, vectorstore
    from app.identity import Principal
    from app.ingest import ingest_corpus
    from app.retrieval import clear_cache, retrieve
    from app.store import engine, init_db
    from qdrant_client.models import Filter

    sample = json.loads(Path(sample_path).read_text())
    corpus = sample["corpus"]
    workdir = Path(tempfile.mkdtemp(prefix="permrag-enron-corpus-"))
    corpus_path = workdir / "corpus.json"
    corpus_path.write_text(json.dumps(corpus))
    init_db()
    t0 = time.perf_counter()
    n_chunks = ingest_corpus(corpus_path)
    ingest_s = time.perf_counter() - t0
    if master_cache and Path(config.EMBED_CACHE_PATH).exists():
        shutil.copy(config.EMBED_CACHE_PATH, master_cache)  # before invariance re-ingests prune it

    def run(text: str, user: str) -> list:
        clear_cache()
        return retrieve(text, Principal(user, ()), k=TOP)["results"]

    run("warm up the model", sample["jobs"][0]["user"])
    results, control, inv = {}, {}, {}
    jobs_to_run = {"all": sample["jobs"], "main": sample["jobs"], "invariance": [],
                   "ranks": [j for j in sample["jobs"] if j["kind"] == "owner"]}[phase]
    for n, j in enumerate(jobs_to_run):
        if n % 100 == 0:
            Path(out_path + ".progress").write_text(f"{rung} {n}/{len(sample['jobs'])} {time.strftime('%H:%M:%S')}\n")
        t = time.perf_counter()
        res = run(j["text"], j["user"])
        results[j["id"]] = {"top": [[r["doc_id"], r["score"]] for r in res],
                            "ms": (time.perf_counter() - t) * 1000}

    # Positive control: the NoFilter bug (ACL filter matches every row) on the first 100 non-reader questions.
    real_filter = vectorstore._acl_filter
    vectorstore._acl_filter = lambda principals: Filter()
    try:
        control = {j["id"]: [[r["doc_id"], r["score"]] for r in run(j["text"], j["user"])]
                   for j in [x for x in sample["jobs"] if x["kind"] == "nonreader"][:100 if phase in ("all", "main") else 0]}
    finally:
        vectorstore._acl_filter = real_filter

    # Hidden-document invariance (destructive: rebuilds the index, so it runs last). Compares doc, text and score.
    if phase in ("all", "invariance"):
        ordered = tied = checks = 0
        full = {}
        for case in sample["invariance"]:
            full[case["user"]] = [[(r["doc_id"], r["text"], r["score"]) for r in run(q, case["user"])]
                                  for q in case["queries"]]
        readers = sample["readers"]
        for case in sample["invariance"]:
            u = case["user"]
            corpus_path.write_text(json.dumps([d for d in corpus if u in readers[d["doc_id"]]]))
            ingest_corpus(corpus_path)
            for q, before in zip(case["queries"], full[u], strict=True):
                after = [(r["doc_id"], r["text"], r["score"]) for r in run(q, u)]
                checks += 1
                ordered += after != before
                tied += not same_up_to_ties(before, after)
        # Control: the SAME full corpus ingested in reverse order (nothing hidden). Any diff here is ingest-order
        # dependence (chunk ids decide exact ties), not hidden-document dependence.
        c0 = sample["invariance"][0]
        corpus_path.write_text(json.dumps(corpus[::-1]))
        ingest_corpus(corpus_path)
        after_all = [[(r["doc_id"], r["text"], r["score"]) for r in run(q, c0["user"])] for q in c0["queries"]]
        order = {"checks": len(after_all), "ordered_diffs": sum(x != y for x, y in zip(after_all, full[c0["user"]])),
                 "diffs_up_to_ties": sum(not same_up_to_ties(y, x) for x, y in zip(after_all, full[c0["user"]]))}
        inv = {"checks": checks, "ordered_diffs": ordered, "diffs_up_to_ties": tied, "reversed_ingest_control": order,
               "users": [c["user"] for c in sample["invariance"]]}

    raw = {"results": results, "control_nofilter": control, "invariance": inv, "ingest_s": round(ingest_s, 1),
           "chunks": n_chunks,
           "config": {"embed_backend": config.EMBED_BACKEND, "sparse_backend": config.SPARSE_BACKEND,
                      "reranker": config.RERANKER, "min_score": config.MIN_SCORE,
                      "rerank_min_score": config.RERANK_MIN_SCORE, "rerank_candidates": config.RERANK_CANDIDATES},
           "models": {"dense": embeddings.model_name() if config.EMBED_BACKEND != "hash" else "hash-ngram-v1",
                      "sparse": embeddings.sparse_model_name() if embeddings.sparse_enabled() else None,
                      "reranker": (config.RERANK_MODEL or "Xenova/ms-marco-MiniLM-L-6-v2")
                      if config.RERANKER != "none" else None}}
    Path(out_path).write_text(json.dumps(raw))
    vectorstore.close_client()
    engine.dispose()


def run_rung(rung: str, sample_path: Path, scratch: Path, master_cache: Path, model_cache: str, phase: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="permrag-rung-") as tmp:
        root = Path(tmp)
        env = rb._rung_env(rung, root, model_cache, {})
        env["TOP_K"] = str(TOP)
        env["RERANK_CANDIDATES"] = "25"
        if master_cache.exists():
            shutil.copy(master_cache, root / "embed_cache.sqlite")
        out = scratch / f"raw-{rung}-{phase}.json"
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_worker", rung, "--_sample",
                               str(sample_path), "--_out", str(out), "--_master", str(master_cache), "--_phase", phase],
                              cwd=PROJECT_ROOT, env=env, text=True, capture_output=True, timeout=6 * 3600)
        if proc.returncode:
            raise RuntimeError(f"rung {rung} worker failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
    return json.loads(out.read_text())


# ---------- table ----------
def _ci(p: dict, key: str = "wilson95") -> str:
    lo, hi = p[key]
    return f"{p['rate'] * 100:.1f}% [{lo * 100:.1f}, {hi * 100:.1f}]"


def _boot(m: dict) -> str:
    lo, hi = m["cluster_bootstrap95"]
    return f"[{lo * 100:.1f}, {hi * 100:.1f}]"


def table(scored: dict[str, dict], meta: dict) -> str:
    rows1, rows2 = [], []
    for name, r in scored.items():
        o = r["owner"]
        c = r["config"]
        floors = f"dense {c['min_score']}" + (f", rerank {c['rerank_min_score']}" if c["reranker"] != "none" else "")
        rows1.append(f"| {name} | {floors} | {_ci(o['hit@1'])} | {_ci(o['hit@4'])} | {_ci(o['hit@10'])} "
                     f"| {o['mrr@10']['mean']:.3f} [{o['mrr@10']['cluster_bootstrap95'][0]:.3f}, "
                     f"{o['mrr@10']['cluster_bootstrap95'][1]:.3f}] | {r['owner_returned_nothing']}/{o['n']} |")
        a, nq, npb = r["leaks_all_nonreader_attempts"], r["nonreader_questions"], r["nonreader_probes"]
        ctl, inv, lat = r["control_nofilter"], r["invariance"], r["latency_owner_ms"]
        rows2.append(f"| {name} | {nq['k']}/{nq['n']} | {npb['k']}/{npb['n']} | {a['k']}/{a['n']} "
                     f"(upper {a['upper95'] * 100:.2f}%) | {ctl['k']}/{ctl['n']} | {inv['diffs_up_to_ties']}/{inv['checks']} (strict {inv['ordered_diffs']}) "
                     f"| {inv['reversed_ingest_control']['diffs_up_to_ties']}/{inv['reversed_ingest_control']['checks']} "
                     f"(strict {inv['reversed_ingest_control']['ordered_diffs']}) "
                     f"| {lat['p50']} / {lat['p95']} | {r['ingest_s']:.0f} |")
    boot = ["| rung | hit@1 | hit@4 | hit@10 |", "|---|---|---|---|"] + [
        f"| {n} | {_boot(r['owner']['hit@1'])} | {_boot(r['owner']['hit@4'])} | {_boot(r['owner']['hit@10'])} |"
        for n, r in scored.items()]
    paired = []
    if meta.get("owner_clusters"):
        pairs = [("dense", "baseline"), ("hybrid", "dense"), ("hybrid_rerank", "hybrid"), ("hybrid_rerank", "dense")]
        paired = ["Paired differences (A - B, percentage points; same questions, mailbox-clustered bootstrap 95% CI). "
                  "An interval that excludes 0 is a real difference on this sample:", "",
                  "| A vs B | hit@1 | hit@10 | MRR@10 (x100) |", "|---|---|---|---|"]
        for x, y in pairs:
            if x in scored and y in scored:
                cells = []
                for k in (1, 10, None):
                    d = paired_diff(scored[x]["owner_ranks"], scored[y]["owner_ranks"], meta["owner_clusters"], k)
                    lo, hi = d["cluster_bootstrap95"]
                    cells.append(f"{d['mean'] * 100:+.1f} [{lo * 100:+.1f}, {hi * 100:+.1f}]")
                paired.append(f"| {x} vs {y} | " + " | ".join(cells) + " |")
        paired.append("")
    out = [f"# Platform retrieval rungs on EnronQA ({meta['date']})", "",
           f"{meta['n_mailboxes']} mailboxes, {meta['n_docs']} emails, {meta['chunks']} platform chunks, "
           f"{meta['n_questions']} machine-written questions, readers derived from email headers. "
           "Owner = the mailbox the question's email came from; a hit is any top-k chunk of the gold email.", "",
           "## Recall (owner queries; Wilson 95% CI in brackets)", "",
           "| rung | floors | hit@1 | hit@4 | hit@10 | MRR@10 [cluster-bootstrap CI] | returned nothing |",
           "|---|---|---|---|---|---|---|", *rows1, "",
           "Mailbox-clustered bootstrap 95% CIs (percent; questions in one mailbox are not independent):", "", *boot, "",
           *paired,
           "## Security and cost", "",
           "| rung | leaks: non-reader questions | leaks: non-reader exact-content probes | all non-reader attempts "
           "| NoFilter control leaks (must be > 0) | hidden-doc diffs, exact-score ties ignored (strict order) | control: same corpus ingested reversed | owner latency p50 / p95 ms | ingest s |",
           "|---|---|---|---|---|---|---|---|---|", *rows2, "",
           f"Environment: {meta['platform']}, Python {meta['python']}, fastembed {meta['fastembed']}, "
           f"qdrant-client {meta['qdrant']}. Seed {meta['seed']}. Latency is end-to-end `retrieve()` (includes the "
           "audit write and the unfiltered denied-count search) on a shared laptop, one run: orders of magnitude only.",
           ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ENRON / "data" / "test-00000-of-00001.parquet"))
    ap.add_argument("--mailboxes", type=int, default=12)
    ap.add_argument("--min-emails", type=int, default=100)
    ap.add_argument("--max-emails", type=int, default=400)
    ap.add_argument("--questions", type=int, default=300)
    ap.add_argument("--probes", type=int, default=60)
    ap.add_argument("--non-readers", type=int, default=3)
    ap.add_argument("--rungs", default=",".join(DEFAULT_RUNGS))
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--scratch", type=Path, default=Path(tempfile.gettempdir()) / "permrag_enron_rungs")
    ap.add_argument("--model-cache", default=str(Path(tempfile.gettempdir()) / "permrag_fastembed_models"))
    ap.add_argument("--_worker")
    ap.add_argument("--_sample")
    ap.add_argument("--_out")
    ap.add_argument("--_master", default="")
    ap.add_argument("--_phase", default="all")
    ap.add_argument("--phase", choices=("all", "main", "invariance", "ranks"), default="all",
                    help="invariance / ranks: rerun only that part and merge it into an existing report.json")
    a = ap.parse_args(argv)
    if a._worker:
        _worker(a._worker, a._sample, a._out, a._master, a._phase)
        return 0
    a.scratch.mkdir(parents=True, exist_ok=True)
    sample = build_sample(a)
    sample_path = a.scratch / f"sample-{a.mailboxes}.json"
    sample_path.write_text(json.dumps(sample))
    print(f"{len(sample['meta']['mailboxes'])} mailboxes, {sample['meta']['n_docs']} emails, "
          f"{len(sample['jobs'])} jobs", file=sys.stderr, flush=True)
    master = a.scratch / f"embed_cache-{a.mailboxes}.sqlite"
    scored, runtimes = {}, {}
    existing = json.loads((a.output_dir / "report.json").read_text()) if a.phase in ("invariance", "ranks") else None
    for rung in a.rungs.split(","):
        t0 = time.time()
        print(f"running {rung} ({a.phase}) ...", file=sys.stderr, flush=True)
        raw = run_rung(rung, sample_path, a.scratch, master, a.model_cache, a.phase)
        if existing and a.phase == "ranks":
            owner = [(j, raw["results"][j["id"]]) for j in sample["jobs"] if j["kind"] == "owner"]
            ranks = [first_rank(res["top"], j["gold"]) for j, res in owner]
            old = existing["rungs"][rung]["owner"]
            for k in KS:  # the rerun must reproduce the main run exactly, or the paired numbers describe another run
                if sum(r is not None and r <= k for r in ranks) != old[f"hit@{k}"]["k"]:
                    raise RuntimeError(f"{rung}: hit@{k} differs from the main run; refusing to merge")
            existing["rungs"][rung]["owner_ranks"] = ranks
            existing["meta"]["owner_clusters"] = [j["user"] for j, _ in owner]
        elif existing:
            existing["rungs"][rung]["invariance"] = raw["invariance"]
        else:
            scored[rung] = score_rung(raw, sample)
        runtimes[rung] = round(time.time() - t0)
        print(f"  {rung} done in {runtimes[rung]}s", file=sys.stderr, flush=True)
    if existing:
        existing["meta"][f"{a.phase}_runtime_s"] = runtimes
        meta, scored, sample_meta = existing["meta"], existing["rungs"], existing["sample"]
    else:
        sample_meta = {k: v for k, v in sample["meta"].items() if k != "question_users"}
    chunks = next(iter(scored.values()))["chunks"]
    meta = existing["meta"] if existing else {"date": time.strftime("%Y-%m-%d"), "platform": platform.platform(), "python": platform.python_version(),
            "fastembed": rb._version("fastembed"), "qdrant": rb._version("qdrant-client"), "seed": enron().SEED,
            "n_mailboxes": len(sample["meta"]["mailboxes"]), "n_docs": sample["meta"]["n_docs"], "chunks": chunks,
            "n_questions": sample["meta"]["n_questions"], "mailboxes": sample["meta"]["mailboxes"],
            "runtime_s": runtimes, "args": {k: v for k, v in vars(a).items() if not k.startswith("_")
                                            and k not in ("scratch", "model_cache", "data", "output_dir")}}
    text = table(scored, meta)
    print(text)
    if a.output_dir:
        a.output_dir.mkdir(parents=True, exist_ok=True)
        (a.output_dir / "table.md").write_text(text)
        report = {"meta": meta, "sample": sample_meta, "rungs": scored}
        (a.output_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=list) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
