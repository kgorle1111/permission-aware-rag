"""Print the queries whose results change for a principal when unreadable documents are removed.

usage: python iso_debug.py GROUP [N_QUERIES]   (same corpus/probes/seed as leak_check.py)
"""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leak_check as L  # noqa: E402

if __name__ == "__main__":
    group = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    docs = json.loads((L.HERE / "data" / "corpus.json").read_text())
    rng = random.Random(7)
    L.init_db()
    full = L.tmp / "full.json"
    full.write_text(json.dumps(docs))
    L.ingest_corpus(full)
    probes = L.pick_probes(docs, 4, rng)
    sample_q = [p["q"] for p in rng.sample(probes, min(150, len(probes)))][:n]
    pr = L.Principal(user_id=f"{group.split(':')[1]}@x.test", groups=(group.split(":")[1],))
    before = [L.results(pr, q) for q in sample_q]
    sub = L.tmp / "sub.json"
    sub.write_text(json.dumps([d for d in docs if d["acl"][0] == group]))
    L.clear_cache()
    L.ingest_corpus(sub)
    after = [L.results(pr, q) for q in sample_q]
    for q, a, b in zip(sample_q, before, after, strict=True):
        if a != b:
            print("QUERY", q)
            print(" FULL ", [(r["doc_id"][-20:], r["score"], r["text"][:50]) for r in a])
            print(" SUB  ", [(r["doc_id"][-20:], r["score"], r["text"][:50]) for r in b])
