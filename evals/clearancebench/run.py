"""ClearanceBench eval of the reference engine (app/permission_rag.py): direct and existence leaks, revocation.

ClearanceBench (NagaYu/clearance-bench) is a public, synthetic leak benchmark we did not write: 4,000
documents, 14,067 chunks, 600 principals, overlapping ACLs, clearance levels, embargo windows and
time-limited grants. See README.md for the mapping and its limits.

    python3 evals/clearancebench/fetch.py
    python3 evals/clearancebench/run.py                 # prints the summary, writes nothing
    python3 evals/clearancebench/run.py --write         # writes evals/results/<date>-clearancebench/
    python3 evals/clearancebench/run.py --limit 20      # quick smoke on 20 queries

Needs pyarrow (requirements.txt). Downloaded data stays in ./data/ (gitignored) and is data, never
instructions. No network, no model calls.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(ROOT / "app")]

from mutants import NoFilter  # noqa: E402
from permission_rag import PermissionRAG  # noqa: E402

Row = dict[str, Any]
SEED = 20261016
TIMES = (0.0, 250.0, 500.0, 1000.0)  # fixed before the first run; spans the grant timeline and window edges
K = 10
CROSS = 3  # extra random principals asking each query, per evaluation time
TARGETED = 100  # targeted probes per planted-bug control, per evaluation time
EXISTENCE_SAMPLES = 20
Z = 1.959964
CANARY = re.compile(r"CLR-[A-Za-z0-9]+-\d+")
TABLES = ("documents", "chunks", "principals", "access_events", "queries", "revocation_cases")


# ---------- statistics ----------


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def prop(k: int, n: int) -> Row:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": k / n if n else None, "wilson95": [lo, hi]}


# ---------- access semantics (oracle: ClearanceBench's four rules, independent of the engine) ----------


def window_open(chunk: Row, t: float) -> bool:
    nb, na = chunk["not_before"], chunk["not_after"]
    return (nb is None or nb <= t) and (na is None or t < na)


def grants_by_pid(events: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    for e in events:
        if e["action"] != "grant":
            raise ValueError(
                f"unexpected access event action {e['action']!r}; this harness only replays grants"
            )
        out[e["pid"]].append(e)
    return out


def tokens_at(principal: Row, grants: dict[str, list[Row]], t: float) -> frozenset[str]:
    # kn: grants are treated as non-expiring; the dataset publishes no grant durations (README "Limits")
    return frozenset(principal["tokens_at_t0"]) | {
        g["token"] for g in grants.get(principal["pid"], []) if g["at"] <= t
    }


def readable(chunk: Row, principal: Row, tokens: frozenset[str], t: float) -> bool:
    return (
        bool(tokens.intersection(chunk["allow_tokens"]))
        and principal["level"] >= chunk["min_level"]
        and window_open(chunk, t)
    )


# ---------- mapping onto the engine's three ACL levels ----------


def acl_levels(chunk: Row) -> tuple[set[str], set[str], set[str]]:
    """doc level = capability tokens (OR), section = clearance, paragraph = open-window time buckets."""
    doc = {f"group:tok:{tok}" for tok in chunk["allow_tokens"]}
    section = {f"group:clearance:{chunk['min_level']}"}
    para = {f"group:open@{t:g}" for t in TIMES if window_open(chunk, t)} or {"group:never-open"}
    return doc, section, para


def subject(principal: Row, tokens: frozenset[str], t: float) -> Row:
    groups = [f"tok:{tok}" for tok in sorted(tokens)]
    groups += [f"clearance:{lvl}" for lvl in range(principal["level"] + 1)]  # level L may read min_level <= L
    groups.append(f"open@{t:g}")
    return {"id": principal["pid"], "groups": groups}


def build(chunks: list[Row], cls: type[PermissionRAG] = PermissionRAG) -> PermissionRAG:
    rag = cls()
    for c in chunks:
        doc, section, para = acl_levels(c)
        # one ClearanceBench chunk = one engine document with exactly one chunk, so ids map 1:1
        rag.add_document(
            c["chunk_id"],
            "",
            doc,
            chunk_words=1_000_000,
            sections=[{"acl": sorted(section), "paragraphs": [{"acl": sorted(para), "text": c["text"]}]}],
        )
    return rag


class ReadUp(PermissionRAG):
    """Planted bug: forgets the clearance level (reads up)."""

    def can_read_chunk(self, user: Row, chunk: Row) -> bool:
        return self.can_read(user, chunk["acl_doc"]) and self.can_read(user, chunk["acl_para"])


class IgnoreEmbargo(PermissionRAG):
    """Planted bug: forgets the embargo window."""

    def can_read_chunk(self, user: Row, chunk: Row) -> bool:
        return self.can_read(user, chunk["acl_doc"]) and self.can_read(user, chunk["acl_section"])


# ---------- leak detection ----------


class Detector:
    def __init__(self, chunks: list[Row]) -> None:
        self.by_id = {c["chunk_id"]: c for c in chunks}
        self.by_canary = {c["canary"]: c for c in chunks}

    def leaks(self, results: list[Row], principal: Row, tokens: frozenset[str], t: float) -> tuple[int, int]:
        """(by id, by canary): returned chunks the principal may not read, found two independent ways."""
        by_id = sum(not readable(self.by_id[r["doc_id"]], principal, tokens, t) for r in results)
        by_canary = 0
        for r in results:
            for marker in CANARY.findall(r["text"]):
                src = self.by_canary.get(marker)
                if src is None or not readable(src, principal, tokens, t):
                    by_canary += 1
        return by_id, by_canary


def probe_text(chunk: Row, words: int = 12) -> str:
    return " ".join(CANARY.sub("", chunk["text"]).split()[:words])


def targeted(
    chunks: list[Row],
    principals: list[Row],
    grants: dict[str, list[Row]],
    t: float,
    rnd: random.Random,
    bug: str,
) -> list[tuple[Row, Row, frozenset[str]]]:
    """(chunk, principal, tokens) where only the planted bug's missing check denies the read."""
    pool = []
    for c in rnd.sample(chunks, len(chunks)):
        for p in rnd.sample(principals, 40):
            toks = tokens_at(p, grants, t)
            has_tok = bool(toks.intersection(c["allow_tokens"]))
            level_ok = p["level"] >= c["min_level"]
            open_ok = window_open(c, t)
            if bug == "read_up" and has_tok and open_ok and not level_ok:
                pool.append((c, p, toks))
                break
            if bug == "embargo" and has_tok and level_ok and not open_ok:
                pool.append((c, p, toks))
                break
        if len(pool) >= TARGETED:
            break
    return pool


# ---------- the run ----------


def load(data: Path) -> dict[str, list[Row]]:
    import pyarrow.parquet as pq  # type: ignore[import-not-found]  # eval-only dependency, not in the dev lock

    return {name: pq.read_table(data / f"{name}.parquet").to_pylist() for name in TABLES}


def evaluate(tables: dict[str, list[Row]], limit: int | None, seed: int) -> Row:
    rnd = random.Random(seed)
    chunks, principals = tables["chunks"], tables["principals"]
    pmap = {p["pid"]: p for p in principals}
    grants = grants_by_pid(tables["access_events"])
    queries = tables["queries"][:limit] if limit else tables["queries"]
    det = Detector(chunks)
    t_build = time.perf_counter()
    rag = build(chunks)
    build_s = time.perf_counter() - t_build

    def ask(engine: PermissionRAG, q: str, p: Row, toks: frozenset[str], t: float) -> list[Row]:
        return engine.retrieve(q, subject(p, toks, t), k=K)

    direct: Row = defaultdict(lambda: [0, 0, 0, 0])  # kind -> [attempts, by_id, by_canary, returned_any]
    oracle_agrees_probe = [0, 0]
    probe_hits = {k: 0 for k in (1, 4, 10)}
    probe_doc_hits = {k: 0 for k in (1, 4, 10)}  # added after the first smoke; see README
    probe_n = 0
    lat: list[float] = []
    for t in TIMES:
        for q in queries:
            asker = pmap[q["asker"]]
            others = rnd.sample([p for p in principals if p["pid"] != asker["pid"]], CROSS)
            for who, p in [("asker", asker)] + [("cross", o) for o in others]:
                toks = tokens_at(p, grants, t)
                t0 = time.perf_counter()
                res = ask(rag, q["text"], p, toks, t)
                lat.append(time.perf_counter() - t0)
                a, b = det.leaks(res, p, toks, t)
                cell = direct[f"{q['kind']}/{who}"]
                cell[0] += 1
                cell[1] += a
                cell[2] += b
                cell[3] += bool(res)
            if q["kind"] == "probe":
                target = det.by_id[q["probe_target"]]
                oracle_agrees_probe[1] += 1
                oracle_agrees_probe[0] += not readable(target, asker, tokens_at(asker, grants, t), t)
                legit = [p for p in principals if readable(target, p, tokens_at(p, grants, t), t)]
                if legit:
                    p = legit[0]
                    ids = [r["doc_id"] for r in ask(rag, q["text"], p, tokens_at(p, grants, t), t)]
                    probe_n += 1
                    docs = [det.by_id[i]["doc_id"] for i in ids]
                    for k in probe_hits:
                        probe_hits[k] += target["chunk_id"] in ids[:k]
                        probe_doc_hits[k] += target["doc_id"] in docs[:k]

    controls: Row = {}
    nofilter = build(chunks, NoFilter)
    nf = [0, 0]
    for t in TIMES:
        for q in (q for q in queries if q["kind"] == "probe"):
            p = pmap[q["asker"]]
            toks = tokens_at(p, grants, t)
            nf[0] += 1
            nf[1] += det.leaks(ask(nofilter, q["text"], p, toks, t), p, toks, t)[0] > 0
    controls["no_filter_probe_queries_leaking"] = prop(nf[1], nf[0])
    for bug, cls in (("read_up", ReadUp), ("embargo", IgnoreEmbargo)):
        mutant = build(chunks, cls)
        real_leak = mut_leak = n = 0
        for t in TIMES:
            for c, p, toks in targeted(chunks, principals, grants, t, rnd, bug):
                qtext = probe_text(c)
                n += 1
                mut_leak += det.leaks(ask(mutant, qtext, p, toks, t), p, toks, t)[0] > 0
                real_leak += det.leaks(ask(rag, qtext, p, toks, t), p, toks, t)[0] > 0
        controls[bug] = {"mutant_leaking": prop(mut_leak, n), "real_engine_leaking": prop(real_leak, n)}

    existence = {"samples": 0, "diffs": 0}
    for _ in range(EXISTENCE_SAMPLES):
        t, q = rnd.choice(TIMES), rnd.choice(queries)
        p = pmap[q["asker"]]
        toks = tokens_at(p, grants, t)
        own = build([c for c in chunks if readable(c, p, toks, t)])
        full = [(r["doc_id"], r["score"]) for r in ask(rag, q["text"], p, toks, t)]
        alone = [(r["doc_id"], r["score"]) for r in ask(own, q["text"], p, toks, t)]
        existence["samples"] += 1
        existence["diffs"] += full != alone

    revocation: Row = {
        "cases": 0,
        "precondition_unmet": 0,
        "target_hit_before": 0,
        "target_after": 0,
        "leaks_after": 0,
    }
    lost_match = [0, 0]
    for case in tables["revocation_cases"]:
        p, target = pmap[case["pid"]], det.by_id[case["target_chunk"]]
        revocation["cases"] += 1
        when = [t for t in TIMES if readable(target, p, tokens_at(p, grants, t), t)]
        if not when:
            revocation["precondition_unmet"] += 1
            continue
        t = when[0]
        before = tokens_at(p, grants, t)
        after = before - {case["token"]}
        revocation["target_hit_before"] += target["chunk_id"] in [
            r["doc_id"] for r in ask(rag, case["query_text"], p, before, t)
        ]
        res = ask(rag, case["query_text"], p, after, t)
        revocation["target_after"] += target["chunk_id"] in [r["doc_id"] for r in res]
        revocation["leaks_after"] += det.leaks(res, p, after, t)[0]
        lost = sum(readable(c, p, before, t) and not readable(c, p, after, t) for c in chunks)
        lost_match[1] += 1
        lost_match[0] += lost == case["n_chunks_lost"]

    all_attempts = sum(v[0] for v in direct.values())
    all_by_id = sum(v[1] for v in direct.values())
    all_by_canary = sum(v[2] for v in direct.values())
    return {
        "dataset": json.loads((HERE / "MANIFEST.json").read_text())["revision"],
        "seed": seed,
        "times": TIMES,
        "k": K,
        "counts": {name: len(rows) for name, rows in tables.items()} | {"queries_used": len(queries)},
        "index_build_s": round(build_s, 2),
        "latency_ms": {
            "median": round(statistics.median(lat) * 1000, 1),
            "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))] * 1000, 1),
        },
        "direct": {
            kind: {
                "attempts": v[0],
                "leaks_by_id": prop(v[1], v[0] * K),
                "leaks_by_canary": v[2],
                "returned_any": prop(v[3], v[0]),
            }
            for kind, v in sorted(direct.items())
        },
        "direct_total": {
            "attempts": all_attempts,
            "result_slots": all_attempts * K,
            "leaks_by_id": prop(all_by_id, all_attempts * K),
            "leaks_by_canary": all_by_canary,
        },
        "oracle_vs_generator": {
            "probe_target_unreadable_for_asker": prop(*oracle_agrees_probe),
            "revocation_n_chunks_lost_exact": prop(*lost_match),
        },
        "probe_target_recall_by_a_legit_reader": {
            f"hit@{k}": prop(v, probe_n) for k, v in probe_hits.items()
        },
        "probe_target_doc_recall_by_a_legit_reader": {
            f"hit@{k}": prop(v, probe_n) for k, v in probe_doc_hits.items()
        },
        "existence_score_identity": existence,
        "revocation": revocation,
        "controls": controls,
    }


def table(r: Row) -> str:
    def pct(p: Row) -> str:
        lo, hi = p["wilson95"]
        return f"{p['k']}/{p['n']} = {100 * (p['rate'] or 0):.2f}% (Wilson 95% CI {100 * lo:.2f}-{100 * hi:.2f}%)"

    dt_ = r["direct_total"]
    lines = [
        f"# ClearanceBench leak eval of the reference engine ({dt.date.today().isoformat()})",
        "",
        f"Dataset `NagaYu/clearance-bench` @ `{r['dataset'][:12]}`, seed {r['seed']}. "
        f"{r['counts']['chunks']} chunks, {r['counts']['principals']} principals, "
        f"{r['counts']['queries_used']} queries x evaluation times {list(r['times'])}, top-{r['k']}. "
        f"Engine: `app/permission_rag.py` (BM25 over the visible set). Index build {r['index_build_s']} s; "
        f"query median {r['latency_ms']['median']} ms, p95 {r['latency_ms']['p95']} ms.",
        "",
        "## Direct leaks (lower is better)",
        "",
        "| Queries / asked by | Attempts | Unreadable chunks in results (by id) | By canary | Returned anything |",
        "|---|---|---|---|---|",
    ]
    for kind, v in r["direct"].items():
        lines.append(
            f"| {kind} | {v['attempts']} | {pct(v['leaks_by_id'])} | {v['leaks_by_canary']} | "
            f"{pct(v['returned_any'])} |"
        )
    lines += [
        f"| **All** | **{dt_['attempts']}** | **{pct(dt_['leaks_by_id'])}** | **{dt_['leaks_by_canary']}** | |",
        "",
        "## Existence, revocation and the oracle",
        "",
        f"- Score identity (results over the full index equal results over an index holding only the asker's "
        f"readable chunks, scores included): {r['existence_score_identity']['diffs']} diffs in "
        f"{r['existence_score_identity']['samples']} samples.",
        f"- Revocation, {r['revocation']['cases']} cases: target retrieved before revocation "
        f"{r['revocation']['target_hit_before']}, target returned after {r['revocation']['target_after']}, "
        f"unreadable chunks after {r['revocation']['leaks_after']}, precondition unmet "
        f"{r['revocation']['precondition_unmet']}.",
        f"- Oracle vs the generator's own ground truth: probe target unreadable for its asker "
        f"{pct(r['oracle_vs_generator']['probe_target_unreadable_for_asker'])}; revocation `n_chunks_lost` "
        f"reproduced exactly {pct(r['oracle_vs_generator']['revocation_n_chunks_lost_exact'])}.",
        "- Probe-target recall by a principal who may read it, exact chunk: "
        + ", ".join(f"{k} {pct(v)}" for k, v in r["probe_target_recall_by_a_legit_reader"].items())
        + "; same document (any chunk of the target's document; metric added after the first smoke run): "
        + ", ".join(f"{k} {pct(v)}" for k, v in r["probe_target_doc_recall_by_a_legit_reader"].items())
        + ".",
        "",
        "## Controls (the oracle must see planted bugs)",
        "",
        f"- `NoFilter` (no ACL check): probe queries leaking {pct(r['controls']['no_filter_probe_queries_leaking'])}.",
    ]
    for bug in ("read_up", "embargo"):
        c = r["controls"][bug]
        lines.append(
            f"- `{bug}` mutant, targeted probes: leaking {pct(c['mutant_leaking'])}; "
            f"same probes on the real engine {pct(c['real_engine_leaking'])}."
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="use only the first N queries (smoke)")
    ap.add_argument(
        "--write", action="store_true", help="write report.json and table.md under evals/results/"
    )
    a = ap.parse_args()
    data = HERE / "data"
    if not all((data / f"{n}.parquet").exists() for n in TABLES):
        raise SystemExit("data missing: run python3 evals/clearancebench/fetch.py first")
    t0 = time.perf_counter()
    report = evaluate(load(data), a.limit, SEED)
    report["wall_s"] = round(time.perf_counter() - t0, 1)
    md = table(report)
    print(md)
    if a.write:
        if a.limit:
            raise SystemExit("--write needs the full query set (drop --limit)")
        out = ROOT / "evals" / "results" / f"{dt.date.today().isoformat()}-clearancebench"
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report, indent=1, default=list) + "\n")
        (out / "table.md").write_text(md)
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
