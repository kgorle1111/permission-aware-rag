"""Retrieval eval harness over the underwriter corpus.

Run: python3 run_evals.py
Metrics: recall@k (expected doc appears in top-k) and leak rate (a must_not
doc appears — this must always be 0, it is the product's core claim).
Exit code 1 on any leak or recall failure, so it can gate CI.
"""

import json
import pathlib
import sys

import underwriter_server as srv

srv.rag.audit_path = None  # evals must not pollute the real audit log

CASES = json.loads((pathlib.Path(__file__).with_name("evals.json")).read_text())
K = 4


def main():
    recall_hits, recall_total, leaks = 0, 0, []
    for case in CASES:
        docs = {r["doc_id"] for r in srv.rag.retrieve(case["q"], srv.USERS[case["role"]], k=K)}
        for want in case["expect"]:
            recall_total += 1
            recall_hits += want in docs
            if want not in docs:
                print(f"MISS  [{case['role']}] {case['q']!r}: expected {want}, got {sorted(docs)}")
        for forbidden in case["must_not"]:
            if forbidden in docs:
                leaks.append((case, forbidden))
                print(f"LEAK  [{case['role']}] {case['q']!r}: returned forbidden {forbidden}")
    print(f"\n{len(CASES)} cases | recall@{K}: {recall_hits}/{recall_total} | leaks: {len(leaks)}")
    return 1 if leaks or recall_hits < recall_total else 0


if __name__ == "__main__":
    sys.exit(main())
