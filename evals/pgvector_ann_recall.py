"""Does approximate nearest-neighbour search lose recall under Postgres RLS? (PLAN F6.4)

pgvector's HNSW scans a fixed candidate list (hnsw.ef_search) and RLS filters AFTER that scan, so a
caller who can see 1% of the rows can get fewer than k results, or miss true neighbours. This script
measures it through the real read path (signed principals -> rag_search -> RLS) on a synthetic corpus:

  * corpus: N clustered unit vectors in the repo's embedding dimension (a Gaussian mixture, seeded);
    each chunk is visible to a "v1" / "v10" / "v60" group independently with p = 1% / 10% / 60%.
  * configs: shipped schema (no ANN index), exact (index present, enable_indexscan=off), HNSW at several
    ef_search values, HNSW with iterative scan (strict_order / relaxed_order).
  * metrics per (user, config): recall@k vs exact, mean results returned, p50/p95 latency, and leaks
    checked against an independent in-Python ACL oracle (never the database's own answer).

    docker run -d --name pg-ann -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:55433:5432 pgvector/pgvector:pg16
    uv run python evals/pgvector_ann_recall.py --dsn postgresql://postgres:postgres@127.0.0.1:55433/postgres \\
        --out evals/results/2026-10-13-pgvector-filtered-ann

It creates a fresh `ann_eval` database and refuses to run if one exists (it never drops anything).
"""

import argparse
import json
import math
import platform
import random
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app")]

import psycopg  # noqa: E402
from embedding import DIMS  # noqa: E402
from pgvector_rag import PgVectorRAG, setup_schema  # noqa: E402

DB = "ann_eval"
SEED = 20261013
CLUSTERS = 200
NOISE = 0.6  # norm of the per-vector noise relative to the unit cluster centre
# visibility level -> (user dict, probability a chunk's ACL includes the user's group)
LEVELS: dict[str, tuple[dict[str, Any], float]] = {
    "1%": ({"id": "u1", "groups": ["v1"]}, 0.01),
    "10%": ({"id": "u10", "groups": ["v10"]}, 0.10),
    "60%": ({"id": "u60", "groups": ["v60"]}, 0.60),
}
BITS = {"v1": 1, "v10": 2, "v60": 4}


@dataclass(frozen=True)
class Config:
    name: str
    index: bool  # run after the HNSW index exists
    settings: tuple[tuple[str, str], ...] = ()


CONFIGS = [
    Config("shipped (no ANN index)", False),
    Config("exact (index present, indexscan off)", True, (("enable_indexscan", "off"),)),
    # The planner never chooses the HNSW index under this RLS policy (it estimates rows=1 and seq-scans),
    # so the forced rows show what HNSW would return if it were used.
    Config("HNSW ef_search=40 (default), planner's choice", True),
    Config("HNSW ef_search=40, forced", True, (("enable_seqscan", "off"),)),
    Config("HNSW ef_search=200, forced", True, (("enable_seqscan", "off"), ("hnsw.ef_search", "200"))),
    Config("HNSW ef_search=1000, forced", True, (("enable_seqscan", "off"), ("hnsw.ef_search", "1000"))),
    Config(
        "HNSW iterative strict_order, forced",
        True,
        (("enable_seqscan", "off"), ("hnsw.iterative_scan", "strict_order")),
    ),
    Config(
        "HNSW iterative relaxed_order, forced",
        True,
        (("enable_seqscan", "off"), ("hnsw.iterative_scan", "relaxed_order")),
    ),
]
EXACT = CONFIGS[1].name
SHIPPED = CONFIGS[0].name


# ── pure helpers (unit-tested in app/test_ann_recall.py) ─────────────────────────────────────────
def recall_at_k(found: Sequence[str], truth: Sequence[str]) -> float:
    """Share of the exact top-k that `found` also returned; an empty truth is trivially recalled."""
    return len(set(found) & set(truth)) / len(truth) if truth else 1.0


def percentile(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile."""
    if not samples:
        raise ValueError("empty measurement")
    return sorted(samples)[math.ceil(fraction * len(samples)) - 1]


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def draw_vector(rnd: random.Random, centre: Sequence[float]) -> list[float]:
    """A unit vector near `centre`: centre + isotropic noise of norm ~NOISE, renormalised."""
    s = NOISE / math.sqrt(len(centre))
    return unit([c + rnd.gauss(0.0, s) for c in centre])


def draw_mask(rnd: random.Random) -> int:
    """Bitmask of the groups a chunk's document ACL names; each is drawn independently."""
    mask = 0
    for name, (_, p) in zip(BITS, LEVELS.values(), strict=True):
        if rnd.random() < p:
            mask |= BITS[name]
    return mask


def acl_for(mask: int) -> list[str]:
    groups = [f"group:{g}" for g, b in BITS.items() if mask & b]
    return sorted(groups) or ["group:none"]  # chunks.acl must be non-empty; nobody holds group:none


def visible(mask: int, user: dict[str, Any]) -> bool:
    """Independent oracle: the chunk's document ACL names one of the user's groups (section and
    paragraph levels are '*' here, so only the document level can deny)."""
    return any(BITS.get(g, 0) & mask for g in user["groups"])


def to_vec(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


def summarize(found: list[list[str]], truth: list[list[str]], ms: list[float], k: int) -> dict[str, Any]:
    recalls = [recall_at_k(f, t) for f, t in zip(found, truth, strict=True)]
    return {
        "recall_at_k": round(sum(recalls) / len(recalls), 4),
        "min_recall": round(min(recalls), 4),
        "mean_returned": round(sum(len(f) for f in found) / len(found), 2),
        "mean_exact_returned": round(sum(len(t) for t in truth) / len(truth), 2),
        "empty_queries": sum(1 for f in found if not f),
        "k": k,
        "p50_ms": round(percentile(ms, 0.50), 2),
        "p95_ms": round(percentile(ms, 0.95), 2),
    }


# ── database side ─────────────────────────────────────────────────────────────────────────────────
@dataclass
class Corpus:
    masks: list[int]
    queries: list[list[float]] = field(default_factory=list)


def make_dsn(base: str, **kw: str) -> str:
    return psycopg.conninfo.make_conninfo(base, **kw)


def load(admin_dsn: str, rows: int, nq: int) -> Corpus:
    rnd = random.Random(SEED)
    centres = [unit([rnd.gauss(0, 1) for _ in range(DIMS)]) for _ in range(CLUSTERS)]
    masks: list[int] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor().copy(
            "COPY chunks (id, doc_id, text, acl, acl_doc, acl_section, acl_para, embedding) FROM STDIN"
        ) as cp:
            for i in range(rows):  # superuser COPY bypasses RLS and the ingest role: bulk-load only
                m = draw_mask(rnd)
                masks.append(m)
                acl = acl_for(m)
                cp.write_row(
                    (
                        f"c{i}",
                        f"d{i}",
                        f"chunk {i}",
                        acl,
                        acl,
                        ["*"],
                        ["*"],
                        to_vec(draw_vector(rnd, rnd.choice(centres))),
                    )
                )
        conn.execute("UPDATE corpus_stats SET chunk_count = %s", (rows,))
        conn.execute("ANALYZE chunks")
    qrnd = random.Random(SEED + 1)
    return Corpus(masks, [draw_vector(qrnd, qrnd.choice(centres)) for _ in range(nq)])


def build_index(admin_dsn: str, m: int, efc: int) -> float:
    t0 = time.perf_counter()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("SET maintenance_work_mem = '512MB'")
        conn.execute("SET max_parallel_maintenance_workers = 0")
        conn.execute(
            f"CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) "
            f"WITH (m = {int(m)}, ef_construction = {int(efc)})"
        )
        conn.execute("ANALYZE chunks")
    return round(time.perf_counter() - t0, 1)


def plan_head(admin_dsn: str, rag: PgVectorRAG, user: dict[str, Any], qvec: str, settings: Config) -> str:
    """Top plan node of rag_search's inner top-k query, planned as its owner role under this user's RLS."""
    token = rag.sign_principals(rag.principals(user))
    with psycopg.connect(admin_dsn) as conn:
        conn.execute("SET LOCAL ROLE rag_definer")
        conn.execute("SELECT set_config('rag.token', %s, true)", (token,))
        for key, val in settings.settings:
            conn.execute(f"SET LOCAL {key} = {val}")
        rows = conn.execute(
            "EXPLAIN SELECT c.id, c.embedding <=> %s::vector AS d FROM chunks c ORDER BY d LIMIT 10", (qvec,)
        ).fetchall()
    return str(next(r[0] for r in rows if "Scan" in r[0])).strip()


def cost_split(admin_dsn: str, rag: PgVectorRAG, corpus: Corpus, k: int) -> dict[str, dict[str, float]]:
    """Median ms of rag_search's two table scans, run as its owner role under each user's RLS (no index):
    the `count(*)` behind `denied_chunks` versus the ORDER BY ... LIMIT k scan."""
    out: dict[str, dict[str, float]] = {}
    qv = to_vec(corpus.queries[0])
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for label, (user, _) in LEVELS.items():
            token = rag.sign_principals(rag.principals(user))
            t_count: list[float] = []
            t_topk: list[float] = []
            for _ in range(15):
                with conn.transaction():
                    conn.execute("SET LOCAL ROLE rag_definer")
                    conn.execute("SELECT set_config('rag.token', %s, true)", (token,))
                    t0 = time.perf_counter()
                    conn.execute("SELECT count(*) FROM chunks").fetchall()
                    t1 = time.perf_counter()
                    conn.execute(
                        "SELECT c.id FROM chunks c ORDER BY c.embedding <=> %s::vector LIMIT %s", (qv, k)
                    ).fetchall()
                    t2 = time.perf_counter()
                t_count.append((t1 - t0) * 1000)
                t_topk.append((t2 - t1) * 1000)
            out[label] = {
                "count_star_p50_ms": round(percentile(t_count, 0.5), 2),
                "order_by_limit_p50_ms": round(percentile(t_topk, 0.5), 2),
            }
    return out


def run_config(
    app_dsn: str, rag: PgVectorRAG, cfg: Config, corpus: Corpus, k: int
) -> dict[str, tuple[list[list[str]], list[float]]]:
    """Fresh session per config: plpgsql caches rag_search's plans per session, so a settings change
    mid-session could silently keep the previous config's plan."""
    out: dict[str, tuple[list[list[str]], list[float]]] = {}
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        for key, val in cfg.settings:
            conn.execute(f"SET {key} = {val}")
        for label, (user, _) in LEVELS.items():
            found: list[list[str]] = []
            ms: list[float] = []
            for qi, q in enumerate(corpus.queries):
                qv = to_vec(q)
                token = rag.sign_principals(rag.principals(user))
                t0 = time.perf_counter()
                rows = conn.execute(
                    "SELECT id FROM rag_search(%s, %s::vector, %s)", (token, qv, k)
                ).fetchall()
                dt = (time.perf_counter() - t0) * 1000
                if qi >= 5:  # first calls warm caches and the plan
                    ms.append(dt)
                found.append([r[0] for r in rows])
            out[label] = (found, ms)
    return out


def scalar(conn: psycopg.Connection, query: str) -> str:
    row = conn.execute(query).fetchone()
    if row is None:
        raise RuntimeError(f"no row from {query!r}")
    return str(row[0])


def count_leaks(found: list[list[str]], corpus: Corpus, user: dict[str, Any]) -> int:
    return sum(1 for ids in found for cid in ids if not visible(corpus.masks[int(cid[1:])], user))


def git_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()


def render_table(report: dict[str, Any]) -> str:
    c = report["corpus"]
    lines = [
        f"# pgvector filtered-ANN recall under RLS, {report['date']}",
        "",
        f"{c['rows']:,} chunks, dim {c['dims']}, {c['clusters']} clusters; {report['queries']} queries per cell, "
        f"k={report['k']}; pgvector {report['versions']['pgvector']}, PostgreSQL {report['versions']['postgres']}; "
        f"HNSW m={report['index']['m']} ef_construction={report['index']['ef_construction']}. "
        "Recall is against exact search through the same `rag_search`. Leaks are checked against an "
        "independent ACL oracle. Embeddings are synthetic (clustered Gaussians), visibility is independent of "
        "the embedding, one connection, no concurrency.",
        "",
        "| visibility (measured) | config | recall@k | min recall | mean results (exact) | empty answers | p50 ms | p95 ms | leaks |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for label, vis in c["visible_fraction"].items():
        for cfg, cell in report["results"][label].items():
            lines.append(
                f"| {label} ({vis:.2%}) | {cfg} | {cell['recall_at_k']:.3f} | {cell['min_recall']:.2f} "
                f"| {cell['mean_returned']:.2f} ({cell['mean_exact_returned']:.2f}) | {cell['empty_queries']} "
                f"| {cell['p50_ms']:.1f} | {cell['p95_ms']:.1f} | {cell['leaks']} |"
            )
    lines += [
        "",
        "Shipped path cost split (median ms, no index, as `rag_definer` under each user's RLS):",
        "",
    ]
    lines += [
        f"- {lv}: `count(*)` for denied_chunks {c['count_star_p50_ms']} ms; ORDER BY ... LIMIT k {c['order_by_limit_p50_ms']} ms"
        for lv, c in report["shipped_cost_split"].items()
    ]
    lines += ["", "Planner choice for the inner top-k query (as `rag_definer`, user u1, first query):", ""]
    lines += [f"- {cfg}: `{plan}`" for cfg, plan in report["plans"].items()]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True, help="superuser DSN of a scratch Postgres with pgvector")
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--hnsw-m", type=int, default=16)
    ap.add_argument("--hnsw-ef-construction", type=int, default=64)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    with psycopg.connect(a.dsn, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB,)).fetchone():
            sys.exit(f"database {DB!r} already exists; drop it yourself if it is yours, then re-run")
        conn.execute(f'CREATE DATABASE "{DB}"')
    admin = make_dsn(a.dsn, dbname=DB)
    key = setup_schema(admin)
    app_dsn = make_dsn(admin, user="rag_app", password="rag_app")
    rag = PgVectorRAG(app_dsn, principal_key=key)

    t0 = time.perf_counter()
    corpus = load(admin, a.rows, a.queries)
    print(f"loaded {a.rows} rows in {time.perf_counter() - t0:.0f}s", flush=True)
    vis_frac = {
        label: round(sum(visible(m, u) for m in corpus.masks) / a.rows, 4) for label, (u, _) in LEVELS.items()
    }

    raw: dict[str, dict[str, tuple[list[list[str]], list[float]]]] = {}
    plans: dict[str, str] = {}
    build_s = 0.0
    qv0 = to_vec(corpus.queries[0])
    for cfg in CONFIGS:
        if cfg.index and not build_s:
            build_s = build_index(admin, a.hnsw_m, a.hnsw_ef_construction)
            print(f"HNSW built in {build_s}s", flush=True)
        t0 = time.perf_counter()
        raw[cfg.name] = run_config(app_dsn, rag, cfg, corpus, a.k)
        if not cfg.index:
            split = cost_split(admin, rag, corpus, a.k)
        plans[cfg.name] = plan_head(admin, rag, LEVELS["1%"][0], qv0, cfg)
        print(f"{cfg.name}: {time.perf_counter() - t0:.0f}s", flush=True)

    results: dict[str, dict[str, Any]] = {}
    for label, (user, _) in LEVELS.items():
        truth = raw[EXACT][label][0]
        if (
            raw[SHIPPED][label][0] != truth
        ):  # shipped = seq scan; it must equal exact or the ground truth is suspect
            print(f"WARNING: shipped != exact at {label}", file=sys.stderr)
        results[label] = {}
        for cfg in CONFIGS:
            found, ms = raw[cfg.name][label]
            cell = summarize(found, truth, ms, a.k)
            cell["leaks"] = count_leaks(found, corpus, user)
            results[label][cfg.name] = cell

    with psycopg.connect(admin) as conn:
        pg_version = scalar(conn, "SHOW server_version")
        vec_version = scalar(conn, "SELECT extversion FROM pg_extension WHERE extname='vector'")
    report = {
        "date": datetime.now(UTC).date().isoformat(),
        "git_commit": git_commit(),
        "versions": {
            "pgvector": vec_version,
            "postgres": pg_version,
            "python": platform.python_version(),
            "machine": platform.platform(),
            "image": "pgvector/pgvector:pg16 (docker, colima)",
        },
        "index": {
            "type": "hnsw",
            "opclass": "vector_cosine_ops",
            "m": a.hnsw_m,
            "ef_construction": a.hnsw_ef_construction,
            "build_seconds": build_s,
            "shipped_schema_has_ann_index": False,
        },
        "corpus": {
            "rows": a.rows,
            "dims": DIMS,
            "clusters": CLUSTERS,
            "noise_norm": NOISE,
            "seed": SEED,
            "query_seed": SEED + 1,
            "visible_fraction": vis_frac,
            "acl": "document-level group per level, independent draws; section/paragraph '*'",
        },
        "queries": a.queries,
        "k": a.k,
        "configs": {c.name: dict(c.settings) | {"index": c.index} for c in CONFIGS},
        "plans": plans,
        "shipped_cost_split": split,
        "results": results,
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "report.json").write_text(json.dumps(report, indent=1) + "\n")
    (a.out / "table.md").write_text(render_table(report))
    print((a.out / "table.md").read_text())
    rag.close()
    return 1 if any(c["leaks"] for lv in results.values() for c in lv.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
