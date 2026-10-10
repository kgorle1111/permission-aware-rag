"""Leak + isolation check on the REAL extracted corpus (text real, ACLs synthetic).

1. exact-content probes: for each document, 12-word shingles that occur in no other
   document are issued as queries by every principal NOT allowed to read it; any
   returned result from a forbidden document is a leak.
2. authorized control: the allowed group issuing the same probe should find the doc
   (so "zero leaks" is not just "zero results").
3. isolation diff: results for a principal over the full corpus must equal results
   over a corpus with every document that principal cannot read removed.

usage: python leak_check.py OUT.json [PROBES_PER_DOC]   (needs data/corpus.json)
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="pdf-leak-"))
os.environ.update(
    DATABASE_URL=f"sqlite:///{tmp}/rag.db",
    QDRANT_PATH=str(tmp / "qdrant"),
    QDRANT_URL="",
    EMBED_BACKEND="hash",
    EMBED_DIM="384",
    ANTHROPIC_API_KEY="",
    CACHE_TTL_S="0",
    PERMISSIONS_BACKEND="jsonl",
    DEMO_MODE="0",
)
sys.path.insert(0, str(HERE.parent.parent / "platform"))
sys.path.insert(0, str(HERE))

from app.identity import Principal  # noqa: E402
from app.ingest import chunk_document, ingest_corpus  # noqa: E402
from app.retrieval import clear_cache, retrieve  # noqa: E402
from app.store import init_db  # noqa: E402
from stats import wilson  # noqa: E402

SHINGLE = 12


def shingles(text: str) -> list[str]:
    w = text.split()
    return [" ".join(w[i : i + SHINGLE]) for i in range(0, max(0, len(w) - SHINGLE), SHINGLE)]


def pick_probes(docs: list[dict], per_doc: int, rng: random.Random) -> list[dict]:
    owner: dict[str, set] = {}
    texts = {}
    for d in docs:
        chunks = chunk_document(d)
        texts[d["doc_id"]] = [c["text"].split("\n", 1)[-1] for c in chunks]
        for t in texts[d["doc_id"]]:
            for s in shingles(re.sub(r"\s+", " ", t)):
                owner.setdefault(s, set()).add(d["doc_id"])
    probes = []
    for d in docs:
        cands = [
            s
            for t in texts[d["doc_id"]]
            for s in shingles(re.sub(r"\s+", " ", t))
            if owner[s] == {d["doc_id"]} and len(s) > 60 and sum(c.isalpha() for c in s) > 40
        ]
        rng.shuffle(cands)
        probes += [{"doc": d["doc_id"], "acl": d["acl"][0], "q": s} for s in cands[:per_doc]]
    return probes


def canon(rs: list[dict]) -> list[tuple]:
    return sorted((-r["score"], r["doc_id"], r["text"]) for r in rs)


def results(principal: Principal, q: str) -> list[dict]:
    return retrieve(q, principal)["results"]


if __name__ == "__main__":
    out = Path(sys.argv[1])
    per_doc = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    docs = json.loads((HERE / "data" / "corpus.json").read_text())
    rng = random.Random(7)
    init_db()
    full = tmp / "full.json"
    full.write_text(json.dumps(docs))
    n_chunks = ingest_corpus(full)
    probes = pick_probes(docs, per_doc, rng)
    groups = sorted({d["acl"][0] for d in docs})
    principals = {
        g: Principal(user_id=f"{g.split(':')[1]}@x.test", groups=(g.split(":")[1],)) for g in groups
    }
    principals["outsider"] = Principal(user_id="eve@x.test", groups=("none",))
    attempts = leaks = 0
    leak_examples = []
    ctrl_n = ctrl_hit = 0
    for p in probes:
        for name, pr in principals.items():
            if name == p["acl"]:
                ctrl_n += 1
                ctrl_hit += any(r["doc_id"] == p["doc"] for r in results(pr, p["q"]))
                continue
            attempts += 1
            got = results(pr, p["q"])
            bad = [r for r in got if r["doc_id"] == p["doc"]]
            if bad:
                leaks += 1
                leak_examples.append({"principal": name, "doc": p["doc"]})
    # isolation diff over a sample of queries, for two principals
    iso = {}
    sample_q = [p["q"] for p in rng.sample(probes, min(150, len(probes)))]
    for name in ("group:tax", "group:examiners"):
        pr = principals[name]
        before = [results(pr, q) for q in sample_q]
        visible = [d for d in docs if d["acl"][0] == name]
        sub = tmp / "sub.json"
        sub.write_text(json.dumps(visible))
        clear_cache()
        ingest_corpus(sub)
        after = [results(pr, q) for q in sample_q]
        iso[name] = {
            "queries": len(sample_q),
            "raw_diffs": sum(a != b for a, b in zip(before, after, strict=True)),
            # equal-score results are ordered by internal chunk id, which differs between the two worlds
            "diffs": sum(canon(a) != canon(b) for a, b in zip(before, after, strict=True)),
        }
        ingest_corpus(full)
        clear_cache()
    lo, hi = wilson(leaks, attempts)
    out.write_text(
        json.dumps(
            {
                "docs": len(docs),
                "chunks": n_chunks,
                "probes": len(probes),
                "principals": len(principals),
                "forbidden_attempts": attempts,
                "leaks": leaks,
                "wilson95": [lo, hi],
                "leak_examples": leak_examples[:10],
                "control_attempts": ctrl_n,
                "control_hits": ctrl_hit,
                "isolation": iso,
            },
            indent=1,
        )
    )
    print(out.read_text())
