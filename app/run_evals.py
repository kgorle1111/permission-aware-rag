"""Retrieval eval harness over the underwriter corpus.

Run: python3 run_evals.py            # label gate + isolation gate on the real retriever
     python3 run_evals.py --mutants  # both gates against every known-leaky retriever

Label gate: recall@k (expected doc in top-k) and leaks (a hand-labeled must_not doc
appears). Isolation gate: for every role and probe query, results over the full corpus
must equal results over a corpus holding only the docs that role can read — ids AND
scores. That oracle needs no labels, so it also catches score side channels.
Exit code 1 on any leak, isolation diff, or recall miss, so it can gate CI.
"""

import json
import pathlib
import sys

import underwriter_server as srv
from mutants import MUTANTS
from permission_rag import PermissionRAG

srv.rag.audit_path = None  # evals must not pollute the real audit log

CASES = json.loads((pathlib.Path(__file__).with_name("evals.json")).read_text())
K = 4

# Valid ACLs that only a buggy matcher would open up. Isolation-gate only: the demo
# corpus and its labels stay unchanged.
TRAPS = [
    (
        "trap-admin",
        "Underwriting admin override codes for Delgado binding exceptions.",
        {"group:underwriting-admin"},
    ),
    (
        "trap-star",
        "Banking star partners list: Delgado Logistics overdraft waiver approved.",
        {"group:banking*"},
    ),
]


def label_gate(rag, verbose=True):
    recall_hits, recall_total, leaks = 0, 0, 0
    for case in CASES:
        docs = {r["doc_id"] for r in rag.retrieve(case["q"], srv.USERS[case["role"]], k=K)}
        for want in case["expect"]:
            recall_total += 1
            recall_hits += want in docs
            if want not in docs and verbose:
                print(f"MISS  [{case['role']}] {case['q']!r}: expected {want}, got {sorted(docs)}")
        for forbidden in case["must_not"]:
            if forbidden in docs:
                leaks += 1
                if verbose:
                    print(f"LEAK  [{case['role']}] {case['q']!r}: returned forbidden {forbidden}")
    return recall_hits, recall_total, leaks


def _build(cls, docs):
    rag = cls()
    for doc_id, text, acl in docs:
        rag.add_document(doc_id, text, acl)
    return rag


def isolation_gate(cls, verbose=True):
    """Count (role, query) probes whose results differ from the role's isolated corpus.

    The reference always uses PermissionRAG.can_read — the spec, not the code under test.
    """
    docs = srv.CORPUS + TRAPS
    probes = [c["q"] for c in CASES] + [text for _, text, _ in docs]  # exact-content probes are the hardest
    rag = _build(cls, docs)
    diffs = 0
    for role, user in srv.USERS.items():
        ref = _build(PermissionRAG, [d for d in docs if PermissionRAG.can_read(user, d[2])])
        for q in probes:
            got = [(r["id"], r["score"]) for r in rag.retrieve(q, user, k=K)]
            want = [(r["id"], r["score"]) for r in ref.retrieve(q, user, k=K)]
            if got != want:
                diffs += 1
                if verbose:
                    print(f"ISOLATION  [{role}] {q[:50]!r}: got {got} want {want}")
    return diffs, len(srv.USERS) * len(probes)


def mutants():
    print(f"{'mutant':<16}{'label leaks':>12}{'isolation diffs':>18}  caught")
    caught = 0
    for cls in MUTANTS:
        _, _, leaks = label_gate(_build(cls, srv.CORPUS), verbose=False)
        diffs, probes = isolation_gate(cls, verbose=False)
        caught += bool(leaks or diffs)
        print(
            f"{cls.__name__:<16}{leaks:>12}{diffs:>12}/{probes:<5}  {'yes' if leaks or diffs else 'NO — gate too weak'}"
        )
    print(f"\ngate recall: {caught}/{len(MUTANTS)} mutants caught")
    return 0 if caught == len(MUTANTS) else 1


def main():
    recall_hits, recall_total, leaks = label_gate(srv.rag)
    diffs, probes = isolation_gate(PermissionRAG)
    print(f"\n{len(CASES)} cases | recall@{K}: {recall_hits}/{recall_total} | leaks: {leaks}")
    print(f"isolation: {diffs}/{probes} probes differ from the role's isolated corpus")
    return 1 if leaks or diffs or recall_hits < recall_total else 0


if __name__ == "__main__":
    sys.exit(mutants() if "--mutants" in sys.argv else main())
