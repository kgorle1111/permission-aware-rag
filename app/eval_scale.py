"""Scaled leak/recall eval on a generated, frozen corpus — enough probes to bound the rate.

Run: python3 eval_scale.py                        # in-memory backend
     DATABASE_URL=... python3 eval_scale.py --pgvector   # Postgres RLS backend (admin DSN)

Corpus: 30 synthetic accounts × 7 doc kinds, each kind behind a different ACL, with
hard negatives (the senior credit memo near-duplicates the banking profile) and trap
ACLs only a buggy matcher would open. Every doc gets 3 probe phrasings, run as every
role. A probe whose target the role can't read is a must-not trial: any unreadable doc
in its top-k is a leak. A probe whose target the role CAN read is a recall trial.

The corpus + probes are hashed; a mismatch with FROZEN_SHA256 aborts, so results are
never silently compared across different eval sets.
"""

import hashlib
import json
import math
import random
import sys

from permission_rag import PermissionRAG
from underwriter_server import USERS

K = 4
SEED = 7
FROZEN_SHA256 = "db6b5dcb51a5fb1d6846986cd9d5a610aeae639242012c01530ce7116bf2a837"

FIRST = "Apex Birch Cobalt Delta Ember Falcon Granite Harbor Iris Juniper Keystone Lumen Meridian Northwind Orion".split()
LAST = "Logistics Foods Marine Textiles Holdings Builders".split()
PEOPLE = (
    "Avery Brooks Casey Devon Ellis Finley Gray Harper Jordan Kendall Logan Morgan Quinn Reese Sawyer".split()
)


def build_corpus():
    rnd = random.Random(SEED)
    names = rnd.sample([f"{a} {b}" for a in FIRST for b in LAST], 30)
    docs = []
    for i, name in enumerate(names):
        slug = name.lower().replace(" ", "-")
        person = rnd.choice(PEOPLE)
        bal, nsf, util = rnd.randint(50, 900) * 1000, rnd.randint(0, 6), rnd.randint(10, 95)
        trap = [{"group:underwriting-admin"}, {"group:banking*"}, {f"user:{person.lower()}"}][i % 3]
        docs += [
            (
                f"{slug}-policy",
                f"Policy {10000 + i} for {name}: status {rnd.choice(['ACTIVE', 'PENDING', 'LAPSED'])}, "
                f"coverage {rnd.randint(1, 40) * 100000} dollars, premium paid quarterly.",
                {"group:underwriting"},
            ),
            (
                f"{slug}-bank",
                f"Bank profile {name}: average balance {bal} dollars, {nsf} NSF events, "
                f"line of credit utilization {util} percent.",
                {"group:banking"},
            ),
            (  # hard negative: same figures as the bank profile, stricter ACL
                f"{slug}-memo",
                f"Credit memo {name}: average balance {bal} dollars, {nsf} NSF events, "
                f"debt service coverage {rnd.randint(80, 160) / 100}, recommend {rnd.choice(['decline', 'collateral', 'premium loading'])}.",
                {"group:senior"},
            ),
            (
                f"{slug}-watch",
                f"Compliance watchlist {name}: principal {person} under review for "
                f"{rnd.choice(['misrepresentation', 'sanctions match', 'undisclosed claims'])}.",
                {"group:compliance"},
            ),
            (
                f"{slug}-audit",
                f"Audit finding {name}: {rnd.choice(['premium leakage', 'missing signatures', 'late endorsements'])} "
                f"sampled in Q{rnd.randint(1, 4)} review.",
                {"group:audit"},
            ),
            (
                f"{slug}-public",
                f"Public filing {name}: incorporated {rnd.randint(1960, 2020)}, "
                f"{rnd.choice(['freight', 'retail', 'manufacturing', 'shipping'])} industry.",
                {"*"},
            ),
            (
                f"{slug}-trap",
                f"Restricted note {name}: override approved by {person} for binding exception.",
                trap,
            ),
        ]
    probes = []
    for doc_id, text, _ in docs:
        words = text.split()
        name = text.split(":")[0]  # "Bank profile Apex Marine" etc.
        probes += [(doc_id, text), (doc_id, name), (doc_id, " ".join(words[-8:]))]
    return docs, probes


def corpus_hash(docs, probes):
    blob = json.dumps({"docs": [(d, t, sorted(a)) for d, t, a in docs], "probes": probes, "k": K})
    return hashlib.sha256(blob.encode()).hexdigest()


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p, d = k / n, 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d


def make_backend(pgvector):
    if not pgvector:
        return PermissionRAG(), "in-memory BM25"
    import os

    import psycopg
    from pgvector_rag import PgVectorRAG, setup_schema

    admin = os.environ["DATABASE_URL"]
    setup_schema(admin)
    with psycopg.connect(admin) as c:  # superuser DSN: bypasses RLS to see the true count
        if c.execute("SELECT count(*) FROM chunks").fetchone()[0]:
            sys.exit("chunks table not empty — point DATABASE_URL at a throwaway database")
    return PgVectorRAG(
        psycopg.conninfo.make_conninfo(admin, user="rag_app", password="rag_app"),
        psycopg.conninfo.make_conninfo(admin, user="rag_ingest", password="rag_ingest"),
    ), "pgvector + RLS"


def main():
    docs, probes = build_corpus()
    h = corpus_hash(docs, probes)
    if h != FROZEN_SHA256:
        sys.exit(f"eval set changed: sha256 {h} != frozen {FROZEN_SHA256} — re-freeze deliberately")
    rag, label = make_backend("--pgvector" in sys.argv)
    acl = {d: a for d, _, a in docs}
    for doc_id, text, a in docs:
        rag.add_document(doc_id, text, a)

    leaks = must_not = hits = recall_n = 0
    for user in USERS.values():
        for target, q in probes:
            got = [r["doc_id"] for r in rag.retrieve(q, user, k=K)]
            if PermissionRAG.can_read(user, acl[target]):
                recall_n += 1
                hits += target in got
            else:
                must_not += 1
                leaked = [d for d in got if not PermissionRAG.can_read(user, acl[d])]
                if leaked:
                    leaks += 1
                    print(f"LEAK [{user['id']}] {q[:60]!r} -> {leaked}", file=sys.stderr)
    _, leak_ub = wilson(leaks, must_not)
    r_lo, r_hi = wilson(hits, recall_n)
    print(
        f"| {label} | {len(docs)} | {leaks}/{must_not} | {leak_ub:.2%} | {hits}/{recall_n} = {hits / recall_n:.1%} | {r_lo:.1%}–{r_hi:.1%} | `{h[:12]}` |"
    )
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
