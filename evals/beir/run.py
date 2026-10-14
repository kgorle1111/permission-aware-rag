"""BEIR benchmark: reference-app BM25 and bge-small vs published numbers, plus the permission tax.

Run from the repo root with the eval venv (see README): `evals/beir/.venv/bin/python evals/beir/run.py`.
Writes report.json + table.md. No network, no paid API; fastembed downloads the bge model once.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import random
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from permission_rag import PermissionRAG  # noqa: E402

from evals.beir.metrics import (  # noqa: E402
    ndcg_at_k,
    paired_bootstrap_ci,
    query_tenant,
    recall_at_k,
    restrict_qrels,
    same_ranking,
    tenant_of,
    wilson_upper,
)

DATASETS = ["scifact", "nfcorpus", "fiqa"]
K_TENANTS, SEED = 4, 0
MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
ONE_CHUNK = 10**9  # one chunk per doc: doc-level BM25, like the published baselines

# Published reference values. BM25: BEIR paper (arXiv:2104.08663) Table 2 (nDCG@10), Table 9 (Recall@100).
# bge: `model-index` metadata of the BAAI/bge-small-en-v1.5 model card (MTEB), percent / 100.
PUBLISHED = {
    "scifact": {"bm25": (0.665, 0.908), "bge": (0.71275, 0.94167)},
    "nfcorpus": {"bm25": (0.325, 0.250), "bge": (0.34305, 0.31135)},
    "fiqa": {"bm25": (0.236, 0.539), "bge": (0.40343, 0.69552)},
}
TENANT_ACL = lambda d: frozenset({f"group:t{tenant_of(d, K_TENANTS, SEED)}"})  # noqa: E731


def load(name: str, limit: int | None):
    d = HERE / "data" / name
    corpus = pq.read_table(d / "corpus.parquet").to_pylist()
    docs = {r["_id"]: (r["title"] + " " + r["text"]).strip() for r in corpus}
    qrels: dict[str, dict[str, int]] = {}
    with (d / "qrels.tsv").open() as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if int(row["score"]) > 0:
                qrels.setdefault(row["query-id"], {})[row["corpus-id"]] = int(row["score"])
    qtext = {r["_id"]: r["text"] for r in pq.read_table(d / "queries.parquet").to_pylist()}
    qids = sorted(q for q in qrels if q in qtext)
    if limit:
        qids = qids[:limit]
    return docs, qids, {q: qtext[q] for q in qids}, {q: qrels[q] for q in qids}


def build_engine(docs: dict[str, str], acl) -> PermissionRAG:
    e = PermissionRAG()
    for doc_id, text in docs.items():
        e.add_document(doc_id, text, acl(doc_id), chunk_words=ONE_CHUNK)
    return e


def bm25_run(e: PermissionRAG, qids, qtext, user) -> dict[str, list[tuple[str, float]]]:
    return {q: [(r["doc_id"], r["score"]) for r in e.retrieve(qtext[q], user, k=100)] for q in qids}


def bge_run(name: str, docs: dict[str, str], qids, qtext):
    from fastembed import TextEmbedding

    model = TextEmbedding(MODEL)
    cache = HERE / "data" / name / "bge_docs.npy"
    ids = list(docs)
    if cache.exists() and cache.stat().st_mtime > (HERE / "data" / name / "corpus.parquet").stat().st_mtime:
        demb = np.load(cache)
    else:
        demb = np.array(list(model.embed([docs[i] for i in ids], batch_size=64)), dtype=np.float32)
        np.save(cache, demb)
    demb /= np.linalg.norm(demb, axis=1, keepdims=True)  # exact cosine == dot product on unit vectors
    out = {}
    for label, prefix in (("bge_noprefix", ""), ("bge_prefix", QUERY_PREFIX)):
        qemb = np.array(list(model.embed([prefix + qtext[q] for q in qids])), dtype=np.float32)
        qemb /= np.linalg.norm(qemb, axis=1, keepdims=True)
        sims = qemb @ demb.T
        top = np.argsort(-sims, axis=1)[:, :100]
        out[label] = {q: [(ids[j], float(sims[i, j])) for j in top[i]] for i, q in enumerate(qids)}
    return out


def score(run, qids, qrels):
    nd = [ndcg_at_k([d for d, _ in run[q]], qrels[q], 10) for q in qids]
    rc = [recall_at_k([d for d, _ in run[q]], qrels[q], 100) for q in qids]
    return nd, rc


def ci(xs, ys=None):
    m, lo, hi = paired_bootstrap_ci(xs, ys)
    return {"mean": m, "ci95": [lo, hi]}


def system_block(name, key, nd, rc, qids):
    pub_nd, pub_rc = PUBLISHED[name][key]
    a, b = ci(nd), ci(rc)
    return {
        "ndcg@10": a,
        "recall@100": b,
        "published_ndcg@10": pub_nd,
        "published_recall@100": pub_rc,
        "delta_ndcg@10": a["mean"] - pub_nd,
        "delta_recall@100": b["mean"] - pub_rc,
        "published_ndcg_inside_ci": a["ci95"][0] <= pub_nd <= a["ci95"][1],
        "per_query_ndcg@10": dict(zip(qids, nd, strict=True)),
    }


def tenant_study(name, docs, qids, qtext, qrels, unfiltered, isolation_n):
    t0 = time.time()
    tenant = lambda d: tenant_of(d, K_TENANTS, SEED)  # noqa: E731
    qt = {q: query_tenant(qrels[q], tenant) for q in qids}
    full = build_engine(docs, TENANT_ACL)
    shards = [
        build_engine({d: t for d, t in docs.items() if tenant(d) == k}, lambda _d: {"*"})
        for k in range(K_TENANTS)
    ]
    filt, leaks_q, leaks_items, items = {}, 0, 0, 0
    for q in qids:
        user = {"id": f"u{qt[q]}", "groups": [f"t{qt[q]}"]}
        filt[q] = [(r["doc_id"], r["score"]) for r in full.retrieve(qtext[q], user, k=100)]
        bad = sum(tenant(d) != qt[q] for d, _ in filt[q])
        leaks_items, items, leaks_q = leaks_items + bad, items + len(filt[q]), leaks_q + (bad > 0)
    inq = {q: restrict_qrels(qrels[q], lambda d, q=q: tenant(d) == qt[q]) for q in qids}
    u_nd, _ = score(unfiltered, qids, qrels)
    u_in, _ = score(unfiltered, qids, inq)
    f_full, _ = score(filt, qids, qrels)
    f_in, f_in_rc = score(filt, qids, inq)
    sample = random.Random(SEED).sample(qids, min(isolation_n, len(qids)))
    diffs = 0
    for q in sample:
        alone = [
            (r["doc_id"], r["score"])
            for r in shards[qt[q]].retrieve(qtext[q], {"id": "x", "groups": []}, k=100)
        ]
        diffs += not same_ranking(filt[q], alone)
    return {
        "k_tenants": K_TENANTS,
        "acl_note": "SYNTHETIC: seeded sha256 hash of doc id -> tenant; not real access structure",
        "queries": len(qids),
        "queries_per_tenant": [sum(v == k for v in qt.values()) for k in range(K_TENANTS)],
        "ndcg@10_unfiltered_full_qrels": ci(u_nd),
        "ndcg@10_unfiltered_in_tenant_qrels": ci(u_in),
        "ndcg@10_filtered_full_qrels": ci(f_full),
        "ndcg@10_filtered_in_tenant_qrels": ci(f_in),
        "recall@100_filtered_in_tenant_qrels": ci(f_in_rc),
        "delta_filtered_in_minus_unfiltered_full": ci(f_in, u_nd),
        "leaks": {
            "queries_with_leak": leaks_q,
            "leaked_items": leaks_items,
            "items_returned": items,
            "wilson95_upper_per_query": wilson_upper(leaks_q, len(qids)),
            "wilson95_upper_per_item": wilson_upper(leaks_items, items),
        },
        "isolation": {
            "sample": len(sample),
            "diffs": diffs,
            "compared": "ids, order and rounded scores, top-100",
        },
        "seconds": round(time.time() - t0, 1),
    }


def fmt(c):
    return f"{c['mean']:.3f} [{c['ci95'][0]:.3f}, {c['ci95'][1]:.3f}]"


def table(rep) -> str:
    L = [f"# BEIR retrieval vs published numbers, {rep['date']}", ""]
    L += [
        "nDCG@10 (mean [paired-bootstrap 95% CI over test queries]). Published BM25: BEIR paper Table 2; "
        "published bge-small: MTEB values in the BAAI/bge-small-en-v1.5 model card.",
        "",
    ]
    L += [
        "| dataset | queries | system | nDCG@10 | published | delta | Recall@100 | published |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for n, r in rep["datasets"].items():
        for key, label in (
            ("bm25_reference_app", "ref-app BM25 (no ACL filter)"),
            ("bge_prefix", "bge-small (MTEB query prefix)"),
            ("bge_noprefix", "bge-small (no prefix, as platform `st`)"),
        ):
            s = r["systems"][key]
            pub = "-" if key == "bge_noprefix" else f"{s['published_ndcg@10']:.3f}"
            pubr = "-" if key == "bge_noprefix" else f"{s['published_recall@100']:.3f}"
            d = "-" if key == "bge_noprefix" else f"{s['delta_ndcg@10']:+.3f}"
            L.append(
                f"| {n} | {r['n_queries']} | {label} | {fmt(s['ndcg@10'])} | {pub} | {d} | {fmt(s['recall@100'])} | {pubr} |"
            )
    L += [
        "",
        "## Permission tax (reference-app BM25, k=4 SYNTHETIC tenants)",
        "",
        "| dataset | nDCG@10 unfiltered (full qrels) | filtered, in-tenant qrels | paired delta | filtered, full qrels | leaks (queries / items) | Wilson 95% upper (per item) | isolation diffs |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for n, r in rep["datasets"].items():
        t = r["permission_tax"]
        lk = t["leaks"]
        L.append(
            f"| {n} | {fmt(t['ndcg@10_unfiltered_full_qrels'])} | {fmt(t['ndcg@10_filtered_in_tenant_qrels'])} | "
            f"{fmt(t['delta_filtered_in_minus_unfiltered_full'])} | {fmt(t['ndcg@10_filtered_full_qrels'])} | "
            f"{lk['queries_with_leak']}/{t['queries']} / {lk['leaked_items']}/{lk['items_returned']} | "
            f"{lk['wilson95_upper_per_item']:.2e} | {t['isolation']['diffs']}/{t['isolation']['sample']} |"
        )
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets", nargs="+", default=DATASETS, choices=DATASETS)
    ap.add_argument("--out", type=Path, default=ROOT / "evals/results/2026-10-12-beir")
    ap.add_argument("--isolation-sample", type=int, default=100)
    ap.add_argument("--limit-queries", type=int, help="smoke test only; do not publish")
    a = ap.parse_args()
    rep = {
        "date": "2026-10-12",
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT
        ).stdout.strip(),
        "versions": {
            "python": platform.python_version(),
            "fastembed": version("fastembed"),
            "numpy": version("numpy"),
            "pyarrow": version("pyarrow"),
            "onnxruntime": version("onnxruntime"),
            "machine": platform.platform(),
        },
        "bge_model": MODEL,
        "manifest": json.loads((HERE / "MANIFEST.json").read_text())["datasets"],
        "datasets": {},
    }
    for name in a.datasets:
        docs, qids, qtext, qrels = load(name, a.limit_queries)
        print(f"[{name}] {len(docs)} docs, {len(qids)} queries", flush=True)
        t = time.time()
        eng = build_engine(docs, lambda _d: {"*"})
        user = {"id": "u", "groups": []}
        bm = bm25_run(eng, qids, qtext, user)
        bm_s = round(time.time() - t, 1)
        del eng
        print(f"[{name}] bm25 {bm_s}s", flush=True)
        t = time.time()
        bge = bge_run(name, docs, qids, qtext)
        bge_s = round(time.time() - t, 1)
        systems = {}
        for key, run, pk in (
            ("bm25_reference_app", bm, "bm25"),
            ("bge_prefix", bge["bge_prefix"], "bge"),
            ("bge_noprefix", bge["bge_noprefix"], "bge"),
        ):
            nd, rc = score(run, qids, qrels)
            systems[key] = system_block(name, pk, nd, rc, qids)
        nd_bm, _ = score(bm, qids, qrels)
        nd_bg, _ = score(bge["bge_prefix"], qids, qrels)
        systems["bge_prefix_minus_bm25_ndcg@10"] = ci(nd_bg, nd_bm)
        rep["datasets"][name] = {
            "n_docs": len(docs),
            "n_queries": len(qids),
            "systems": systems,
            "seconds": {"bm25": bm_s, "bge_embed_and_search": bge_s},
        }
        print(f"[{name}] bge {bge_s}s; tenant study", flush=True)
        rep["datasets"][name]["permission_tax"] = tenant_study(
            name, docs, qids, qtext, qrels, bm, a.isolation_sample
        )
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "report.json").write_text(json.dumps(rep, indent=1) + "\n")
    (a.out / "table.md").write_text(table(rep))
    print(table(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
