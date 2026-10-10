"""EnronQA eval of the reference engine (app/permission_rag.py): leaks, recall, citation validity.

Real emails and real questions we did not write (EnronQA, arXiv:2505.00263). Permissions come from
the corpus structure: an email is readable by the mailbox it was collected from. See README.md.

    python3 evals/enronqa/run.py --no-llm                 # everything except the paid step
    python3 evals/enronqa/run.py --smoke 5                # 5 paid questions: prints cost/question, writes nothing
    python3 evals/enronqa/run.py --llm-n 600 --max-usd 5  # full run, hard spend cap

The key is read from the environment only (llm.ask); this file never prints or opens it.
Needs pyarrow (evals/enronqa/requirements.txt). Downloaded text stays in ./data/ (gitignored).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "evals")]

from metrics import _tokens  # noqa: E402
from permission_rag import PermissionRAG, tokenize  # noqa: E402

SEED = 20261010
KS = (1, 4, 10)
LLM_K = 4  # chunks handed to the LLM
Z = 1.959964
HEADER_SEP = "=====================================\n"
# claude-haiku-4-5 list price, USD per token (cache write 1.25x input, cache read 0.1x input)
PRICE_IN, PRICE_OUT = 1.00 / 1e6, 5.00 / 1e6
MAX_OUT_TOKENS = 600  # llm.MAX_TOKENS
SUPPORT_THRESHOLD = 0.6  # fixed a priori, not tuned
_CITE = re.compile(r"\[([^\[\]\r\n]+)\]")


# ---------- statistics ----------
def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials; (0, 1) when n == 0."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def bootstrap_ci(values, clusters=None, b: int = 2000, seed: int = SEED):
    """Percentile bootstrap CI of the mean. With `clusters`, resample whole clusters (mailboxes)."""
    values = list(values)
    if not values:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    if clusters is None:
        groups = [[v] for v in values]  # plain bootstrap = every item its own cluster
    else:
        by = collections.defaultdict(list)
        for v, c in zip(values, clusters, strict=True):
            by[c].append(v)
        groups = list(by.values())
    means = []
    for _ in range(b):
        picked = [v for g in rnd.choices(groups, k=len(groups)) for v in g]
        means.append(sum(picked) / len(picked))
    means.sort()
    return (means[int(0.025 * b)], means[int(0.975 * b) - 1])


def prop(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": k / n if n else None, "wilson95": [lo, hi]}


# ---------- data and ACL mapping ----------
def body_of(email: str) -> str:
    return email.split(HEADER_SEP, 1)[-1].strip()


def build_acl(rows: list[dict]) -> dict[str, dict]:
    """doc_id -> {"text", "owners"}. One doc per distinct body; a body found in several mailboxes
    keeps the first path (sorted) as its id and its readers are the UNION of those mailboxes."""
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for r in sorted(rows, key=lambda r: r["path"]):
        groups[hashlib.sha256(body_of(r["email"]).encode()).hexdigest()].append(r)
    docs = {}
    for g in groups.values():
        docs[g[0]["path"]] = {
            "text": g[0]["email"],
            "owners": {r["user"] for r in g},
            "paths": [r["path"] for r in g],
        }
    return docs


def acl_for(owners) -> list[str]:
    return sorted(f"user:{u}" for u in owners)


def readable(owners_by_doc: dict[str, set[str]], user: str, doc_id: str) -> bool:
    """The oracle: independent of PermissionRAG. Unknown docs are unreadable (fail closed)."""
    return user in owners_by_doc.get(doc_id, ())


def count_leaks(results: list[dict], user: str, owners_by_doc) -> int:
    bad = 0
    for r in results:
        if not (
            readable(owners_by_doc, user, r["doc_id"])
            and readable(owners_by_doc, user, r["id"].rsplit("#", 1)[0])
        ):
            bad += 1
    return bad


def load_rows(path: Path) -> list[dict]:
    import pyarrow.parquet as pq  # eval-only dependency

    cols = ["email", "path", "user", "questions", "gold_answers", "alternate_answers"]
    return pq.read_table(path, columns=cols).to_pylist()


def sample_mailboxes(rows, n: int, min_emails: int, seed: int) -> list[str]:
    counts = collections.Counter(r["user"] for r in rows)
    eligible = sorted(u for u, c in counts.items() if c >= min_emails)
    return sorted(random.Random(seed).sample(eligible, n))


def make_questions(rows, doc_of_path: dict[str, str], n_q: int, seed: int) -> list[dict]:
    qs = []
    for r in rows:
        alts = r.get("alternate_answers") or []
        for i, q in enumerate(r["questions"] or []):
            golds = [r["gold_answers"][i]] + list(alts[i] if i < len(alts) else [])
            qs.append({"user": r["user"], "gold_doc": doc_of_path[r["path"]], "question": q, "golds": golds})
    qs.sort(key=lambda q: (q["user"], q["gold_doc"], q["question"]))
    random.Random(seed).shuffle(qs)
    return qs[:n_q]


# ---------- retrieval metrics ----------
def hit_ks(results: list[dict], gold_doc: str, ks=KS) -> dict[int, bool]:
    return {k: any(r["doc_id"] == gold_doc for r in results[:k]) for k in ks}


def distinctive_shingle(text: str, df: collections.Counter, n_docs: int, width: int = 8) -> str:
    """The `width`-token window of the body with the highest summed IDF (rare, exact content)."""
    toks = tokenize(body_of(text))
    if len(toks) <= width:
        return " ".join(toks)
    idf = [math.log(n_docs / (1 + df[t])) for t in toks]
    best = max(range(len(toks) - width + 1), key=lambda i: sum(idf[i : i + width]))
    return " ".join(toks[best : best + width])


# ---------- answer scoring (SQuAD-style) ----------
def normalize(s: str) -> str:
    s = re.sub(r"\b(a|an|the)\b", " ", re.sub(r"[^a-z0-9\s]", " ", s.lower()))
    return " ".join(s.split())


def f1(pred: str, gold: str) -> float:
    p, g = normalize(pred).split(), normalize(gold).split()
    common = collections.Counter(p) & collections.Counter(g)
    same = sum(common.values())
    if not p or not g or not same:
        return 0.0
    prec, rec = same / len(p), same / len(g)
    return 2 * prec * rec / (prec + rec)


def best_f1_em(pred: str, golds: list[str]) -> tuple[float, bool]:
    return max(f1(pred, g) for g in golds), any(normalize(pred) == normalize(g) for g in golds)


def supports(cited_text: str, golds: list[str], question: str) -> bool | None:
    """Deterministic: >= SUPPORT_THRESHOLD of a gold answer's content tokens (minus question tokens)
    occur in the cited text. None when no gold answer has content tokens left to test."""
    qt, ct = _tokens(question), _tokens(cited_text)
    scored = [len(a & ct) / len(a) for g in golds if (a := _tokens(g) - qt)]
    return max(scored) >= SUPPORT_THRESHOLD if scored else None


def usage_cost(u: dict) -> float:
    inp = (
        u.get("input_tokens", 0)
        + 1.25 * u.get("cache_creation_input_tokens", 0)
        + 0.1 * u.get("cache_read_input_tokens", 0)
    )
    return inp * PRICE_IN + u.get("output_tokens", 0) * PRICE_OUT


def score_answer(q: dict, answer: str, retrieved: list[dict], owners_by_doc) -> dict:
    """Citation validity for one answer. `retrieved` = the chunks the LLM was given."""
    cites = sorted(set(_CITE.findall(answer)))
    got = {c["doc_id"] for c in retrieved}
    valid = [c for c in cites if c in got and readable(owners_by_doc, q["user"], c)]
    sup = [
        supports(" ".join(c["text"] for c in retrieved if c["doc_id"] == d), q["golds"], q["question"])
        for d in valid
    ]
    clean = _CITE.sub("", answer)
    f, em = best_f1_em(clean, q["golds"])
    return {
        "cites": len(cites),
        "valid": len(valid),
        "support_testable": sum(s is not None for s in sup),
        "supported": sum(bool(s) for s in sup),
        "f1": f,
        "em": em,
        "refusal": "do not answer this" in answer,
    }


def run_llm(qs, results_by_q, owners_by_doc, max_usd: float, ask, log=print) -> dict:
    spent, rows, aborted = 0.0, [], None
    for i, q in enumerate(qs):
        ctx = results_by_q[i][:LLM_K]
        worst = (sum(len(c["text"]) for c in ctx) / 3 + 800) * PRICE_IN + MAX_OUT_TOKENS * PRICE_OUT
        if spent + worst > max_usd:
            aborted = (
                f"stopped before question {i}: spent ${spent:.4f} + worst-case ${worst:.4f} > cap ${max_usd}"
            )
            break
        out = ask(q["question"], ctx)
        if out is None:
            raise SystemExit("ANTHROPIC_API_KEY is not set; rerun with --no-llm or set it in the environment")
        spent += usage_cost(out["usage"])
        rows.append(score_answer(q, out["answer"], ctx, owners_by_doc) | {"user": q["user"]})
        if (i + 1) % 25 == 0:
            log(f"  llm {i + 1}/{len(qs)} spent ${spent:.4f}")
    return {"rows": rows, "spent_usd": spent, "aborted": aborted}


def summarize_llm(res: dict) -> dict:
    rows = res["rows"]
    if not rows:
        return {"status": "no rows", "spent_usd": res["spent_usd"], "aborted": res["aborted"]}
    cites, valid = sum(r["cites"] for r in rows), sum(r["valid"] for r in rows)
    testable, supported = sum(r["support_testable"] for r in rows), sum(r["supported"] for r in rows)
    cl = [r["user"] for r in rows]
    cited_rows = [r for r in rows if r["cites"]]
    return {
        "status": "ok",
        "n_questions": len(rows),
        "spent_usd": round(res["spent_usd"], 4),
        "cost_per_question_usd": round(res["spent_usd"] / len(rows), 5),
        "aborted": res["aborted"],
        "answers_without_citation": prop(
            sum(1 for r in rows if not r["cites"] and not r["refusal"]), len(rows)
        ),
        "refusals": prop(sum(r["refusal"] for r in rows), len(rows)),
        "citation_retrieved_and_readable": prop(valid, cites),
        "answers_with_all_citations_valid": prop(
            sum(r["valid"] == r["cites"] for r in cited_rows), len(cited_rows)
        ),
        "valid_citation_supports_gold": prop(supported, testable),
        "exact_match": prop(sum(r["em"] for r in rows), len(rows)),
        "f1_mean": statistics.mean(r["f1"] for r in rows),
        "f1_mean_ci95_cluster_bootstrap": bootstrap_ci([r["f1"] for r in rows], cl),
    }


# ---------- main ----------
def pctl(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data" / "test-00000-of-00001.parquet"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "results" / "2026-10-10-enronqa"))
    ap.add_argument("--mailboxes", type=int, default=24)
    ap.add_argument("--min-emails", type=int, default=100)
    ap.add_argument("--questions", type=int, default=600)
    ap.add_argument("--non-owners", type=int, default=3)
    ap.add_argument("--probes", type=int, default=600)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--llm-n", type=int, default=0, help="questions sent to the paid LLM step")
    ap.add_argument(
        "--smoke", type=int, default=0, help="paid smoke run on N questions; prints cost, writes nothing"
    )
    ap.add_argument("--max-usd", type=float, default=5.0)
    a = ap.parse_args(argv)
    t_start = time.time()

    rows_all = load_rows(Path(a.data))
    mailboxes = sample_mailboxes(rows_all, a.mailboxes, a.min_emails, SEED)
    rows = [r for r in rows_all if r["user"] in set(mailboxes)]
    docs = build_acl(rows)
    owners_by_doc = {d: v["owners"] for d, v in docs.items()}
    doc_of_path = {p: d for d, v in docs.items() for p in v["paths"]}
    qs = make_questions(rows, doc_of_path, a.questions, SEED)
    print(f"{len(mailboxes)} mailboxes, {len(docs)} docs, {len(qs)} questions", flush=True)

    rag = PermissionRAG()
    t0 = time.time()
    for d, v in docs.items():
        rag.add_document(d, v["text"], acl_for(v["owners"]))
    ingest_s = time.time() - t0
    base = PermissionRAG()  # no-ACL baseline: same chunks and statistics, every chunk public
    pub = frozenset({"*"})
    base.chunks = [{**c, "acl": pub, "acl_doc": pub} for c in rag.chunks]
    print(f"indexed {len(rag.chunks)} chunks in {ingest_s:.0f}s", flush=True)

    user = lambda u: {"id": u, "groups": []}  # noqa: E731
    rnd = random.Random(SEED + 1)
    if a.smoke:
        qs = qs[: a.smoke]

    # --- owner queries: recall (ACL vs no-ACL) and owner-side leaks
    times, owner_leaks, hits, bhits, owner_res = [], 0, [], [], []
    for q in qs:
        t = time.perf_counter()
        res = rag.retrieve(q["question"], user(q["user"]), k=max(KS))
        times.append(time.perf_counter() - t)
        owner_leaks += count_leaks(res, q["user"], owners_by_doc)
        owner_res.append(res)
        hits.append(hit_ks(res, q["gold_doc"]))
        bhits.append(hit_ks(base.retrieve(q["question"], user(q["user"]), k=max(KS)), q["gold_doc"]))
    print(f"owner queries done; median {statistics.median(times) * 1000:.0f} ms", flush=True)

    report = {
        "dataset": json.loads((HERE / "MANIFEST.json").read_text()) | {"split_used": "test"},
        "seed": SEED,
        "params": {k: v for k, v in vars(a).items() if k not in ("data", "out")},
        "sample": {
            "mailboxes": mailboxes,
            "n_mailboxes": len(mailboxes),
            "n_docs": len(docs),
            "n_chunks": len(rag.chunks),
            "n_words": sum(len(v["text"].split()) for v in docs.values()),
            "multi_owner_docs": sum(len(v["owners"]) > 1 for v in docs.values()),
            "n_questions": len(qs),
            "chunk_words": 80,
            "ingest_seconds": round(ingest_s, 1),
        },
        "latency_ms_owner_query": {
            "median": statistics.median(times) * 1000,
            "p95": pctl(times, 0.95) * 1000,
        },
    }
    cl = [q["user"] for q in qs]
    report["recall"] = {
        "definition": "hit@k = some chunk of the gold source email (the email the question was generated from) in the top-k chunks, querying as the mailbox owner",
        "with_acl": {
            f"hit@{k}": prop(sum(h[k] for h in hits), len(hits))
            | {"cluster_bootstrap95": bootstrap_ci([h[k] for h in hits], cl)}
            for k in KS
        },
        "no_acl_baseline": {
            f"hit@{k}": prop(sum(h[k] for h in bhits), len(bhits))
            | {"cluster_bootstrap95": bootstrap_ci([h[k] for h in bhits], cl)}
            for k in KS
        },
        "paired_acl_only_hits_vs_baseline_only_hits@4": [
            sum(h[4] and not b[4] for h, b in zip(hits, bhits, strict=True)),
            sum(b[4] and not h[4] for h, b in zip(hits, bhits, strict=True)),
        ],
    }

    if not a.smoke:
        # --- non-owner queries and exact-content probes
        owners_all = sorted(mailboxes)
        df = collections.Counter()
        for v in docs.values():
            df.update(set(tokenize(body_of(v["text"]))))
        nq_leaks = nq_attempts = nq_returned = 0
        for q in qs:
            others = rnd.sample(
                [u for u in owners_all if u not in owners_by_doc[q["gold_doc"]]], a.non_owners
            )
            for u in others:
                res = rag.retrieve(q["question"], user(u), k=max(KS))
                nq_attempts += 1
                nq_returned += bool(res)
                nq_leaks += count_leaks(res, u, owners_by_doc)
        probe_docs = rnd.sample(sorted(docs), min(a.probes, len(docs)))
        p_leaks = p_attempts = p_returned = p_owner_hit = 0
        for d in probe_docs:
            shingle = distinctive_shingle(docs[d]["text"], df, len(docs))
            owner = sorted(owners_by_doc[d])[0]
            p_owner_hit += any(r["doc_id"] == d for r in rag.retrieve(shingle, user(owner), k=1))
            for u in rnd.sample([u for u in owners_all if u not in owners_by_doc[d]], a.non_owners):
                res = rag.retrieve(shingle, user(u), k=max(KS))
                p_attempts += 1
                p_returned += bool(res)
                p_leaks += count_leaks(res, u, owners_by_doc)
        report["leaks"] = {
            "definition": "a leak = a returned chunk whose doc the querying user is not in owners_by_doc for (oracle independent of the engine); top-10 inspected per query",
            "owner_queries": prop(owner_leaks, len(qs)) | {"leaked_chunks": owner_leaks},
            "non_owner_question_queries": prop(nq_leaks, nq_attempts)
            | {"queries_returning_anything": nq_returned},
            "non_owner_exact_content_probes": prop(p_leaks, p_attempts)
            | {"queries_returning_anything": p_returned, "probe_width_tokens": 8},
            "probe_sensitivity_control": {
                "note": "same probes as the owner, top-1: shows the probe text really retrieves its email",
                **prop(p_owner_hit, len(probe_docs)),
            },
        }
        tot_k = owner_leaks + nq_leaks + p_leaks
        tot_n = len(qs) + nq_attempts + p_attempts
        report["leaks"]["all_attempts"] = prop(tot_k, tot_n)

        # --- positive control: the harness must see a leak when one exists
        from mutants import NoFilter

        leaky = NoFilter()
        leaky.chunks = rag.chunks
        ctl_k = ctl_n = 0
        for q in qs[:100]:
            u = rnd.choice([u for u in owners_all if u not in owners_by_doc[q["gold_doc"]]])
            ctl_n += 1
            ctl_k += count_leaks(leaky.retrieve(q["question"], user(u), k=max(KS)), u, owners_by_doc) > 0
        report["leaks"]["positive_control_NoFilter_mutant"] = prop(ctl_k, ctl_n) | {
            "note": "queries (of 100) on which the planted no-ACL-check mutant returned a leaked chunk; must be > 0. This eval inspects returned chunks only; score side channels are covered by run_evals.py"
        }

        # --- heuristic validity check for the 'supports' rule (no LLM)
        sup = [supports(docs[q["gold_doc"]]["text"], q["golds"], q["question"]) for q in qs]
        testable = [s for s in sup if s is not None]
        report["support_rule_on_gold_email_text"] = prop(sum(testable), len(testable))

    # --- paid step
    n_llm = a.smoke or (0 if a.no_llm else a.llm_n)
    if n_llm:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            report["citation_validity"] = {"status": "skipped: ANTHROPIC_API_KEY not set in the environment"}
            print(report["citation_validity"]["status"])
        else:
            import llm

            res = run_llm(qs[:n_llm], owner_res, owners_by_doc, a.max_usd, llm.ask)
            report["citation_validity"] = summarize_llm(res)
            report["citation_validity"]["model"] = llm.MODEL
            report["citation_validity"]["pricing_usd_per_mtok"] = {
                "input": PRICE_IN * 1e6,
                "output": PRICE_OUT * 1e6,
            }
            report["citation_validity"]["support_threshold"] = SUPPORT_THRESHOLD
    else:
        report["citation_validity"] = {"status": "not run (--no-llm or --llm-n 0)"}

    if a.smoke:
        print(json.dumps(report["citation_validity"], indent=1))
        return 0
    report["runtime_seconds"] = round(time.time() - t_start)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=1) + "\n")
    (out / "table.md").write_text(render_table(report))
    print(f"wrote {out}/report.json and table.md")
    return 0


def fmt(p: dict) -> str:
    lo, hi = p["wilson95"]
    return (
        f"{p['k']}/{p['n']} = {100 * p['rate']:.2f}% (Wilson 95% CI {100 * lo:.2f}-{100 * hi:.2f}%)"
        if p["n"]
        else "n/a"
    )


def render_table(r: dict) -> str:
    s, rec, lk = r["sample"], r["recall"], r["leaks"]
    L = [
        "# EnronQA permission-aware retrieval eval (2026-10-10)",
        "",
        f"Dataset `{r['dataset']['dataset']}` @ `{r['dataset']['revision'][:12]}` (test split), seed {r['seed']}. "
        f"{s['n_mailboxes']} mailboxes, {s['n_docs']} emails, {s['n_chunks']} chunks ({s['n_words']} words), "
        f"{s['n_questions']} questions. Engine: `app/permission_rag.py`, BM25 over the visible set, 80-word chunks.",
        f"Median owner query {r['latency_ms_owner_query']['median']:.0f} ms (p95 {r['latency_ms_owner_query']['p95']:.0f} ms) at this index size.",
        "",
        "## Leaks (lower is better)",
        "",
        "| Attempt type | Result |",
        "|---|---|",
        f"| Owner queries | {fmt(lk['owner_queries'])} |",
        f"| Non-owner, EnronQA question (3 per question) | {fmt(lk['non_owner_question_queries'])} |",
        f"| Non-owner, exact 8-token content probe (3 per probe) | {fmt(lk['non_owner_exact_content_probes'])} |",
        f"| **All attempts** | **{fmt(lk['all_attempts'])}** |",
        "",
        f"Controls: probe text retrieves its own email as the owner at top-1 in {fmt(lk['probe_sensitivity_control'])}; "
        f"the planted `NoFilter` mutant leaks on {lk['positive_control_NoFilter_mutant']['k']}/{lk['positive_control_NoFilter_mutant']['n']} non-owner queries.",
        "",
        "## Recall, querying as the owner (higher is better)",
        "",
        "| k | With ACL | No-ACL baseline |",
        "|---|---|---|",
    ]
    for k in KS:
        a, b = rec["with_acl"][f"hit@{k}"], rec["no_acl_baseline"][f"hit@{k}"]
        L.append(
            f"| hit@{k} | {100 * a['rate']:.1f}% (Wilson {100 * a['wilson95'][0]:.1f}-{100 * a['wilson95'][1]:.1f}; "
            f"mailbox-bootstrap {100 * a['cluster_bootstrap95'][0]:.1f}-{100 * a['cluster_bootstrap95'][1]:.1f}) "
            f"| {100 * b['rate']:.1f}% (Wilson {100 * b['wilson95'][0]:.1f}-{100 * b['wilson95'][1]:.1f}; "
            f"mailbox-bootstrap {100 * b['cluster_bootstrap95'][0]:.1f}-{100 * b['cluster_bootstrap95'][1]:.1f}) |"
        )
    L += ["", "## Citation validity (paid LLM step)", ""]
    cv = r["citation_validity"]
    if cv.get("status") != "ok":
        L.append(f"Not measured: {cv.get('status')}.")
    else:
        L += [
            f"Model `{cv['model']}`, {cv['n_questions']} questions, spent ${cv['spent_usd']} (${cv['cost_per_question_usd']}/question).",
            "",
            "| Metric | Result |",
            "|---|---|",
            f"| Citations that point to a retrieved AND readable doc | {fmt(cv['citation_retrieved_and_readable'])} |",
            f"| Answers whose every citation is valid | {fmt(cv['answers_with_all_citations_valid'])} |",
            f"| Valid citations whose retrieved text supports the gold answer | {fmt(cv['valid_citation_supports_gold'])} |",
            f"| Exact match vs gold | {fmt(cv['exact_match'])} |",
            f"| Mean token F1 vs gold (mailbox-bootstrap 95% CI) | {cv['f1_mean']:.3f} ({cv['f1_mean_ci95_cluster_bootstrap'][0]:.3f}-{cv['f1_mean_ci95_cluster_bootstrap'][1]:.3f}) |",
            f"| Refusals | {fmt(cv['refusals'])} |",
        ]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
