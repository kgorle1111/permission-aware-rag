"""Compare retrieval observations against an independent authorization specification."""

import math
import re
from collections.abc import Callable

LEVELS = ("acl_doc", "acl_section", "acl_para")
ENTRY = re.compile(r"\*|(?:user|group):[^,\s]+")


def allowed(chunk: dict, principal: dict) -> bool:
    """OR within a valid nonempty level, AND across three required levels.

    Deliberately imports no application matcher or normalization function.
    """
    scope = {"*", f"user:{principal['id']}", *(f"group:{g}" for g in principal["groups"])}
    for level in LEVELS:
        acl = chunk.get(level)
        if not isinstance(acl, list) or not acl:
            return False
        if any(not isinstance(p, str) or not ENTRY.fullmatch(p) for p in acl):
            return False
        if not scope.intersection(acl):
            return False
    return True


def observe(adapter, query, principal, k):
    rows = adapter.retrieve(query, principal, k)
    if not isinstance(rows, list) or len(rows) > k:
        raise ValueError("adapter must return a list of at most k hits")
    seen, result = set(), []
    for row in rows:
        chunk_id, score = row["id"], row["score"]
        if not isinstance(chunk_id, str) or chunk_id in seen:
            raise ValueError("hit IDs must be unique strings")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("hit scores must be finite numbers")
        seen.add(chunk_id)
        result.append((chunk_id, score))
    return result


def run_suite(factory: Callable, fixture: dict) -> dict:
    """Errors and missing required hits fail; an always-empty adapter cannot pass.

    The visible comparison uses the same adapter/ranking implementation on an
    independently authorized corpus. It never invokes the tested ACL matcher to
    decide which chunks belong in that corpus.
    """
    chunks = fixture["chunks"]
    ids = [c["id"] for c in chunks]
    if len(ids) != len(set(ids)) or not chunks:
        raise ValueError("fixture needs nonempty chunks with unique IDs")
    report = {
        "probes": 0,
        "visibility_failures": 0,
        "isolation_failures": 0,
        "positive_controls": 0,
        "recall_misses": 0,
        "errors": 0,
        "invalid_acl_controls": 0,
        "invalid_acl_failures": 0,
        "revocation_controls": 0,
        "revocation_failures": 0,
        "examples": [],
    }

    def fail(kind, **details):
        report[kind] += 1
        if len(report["examples"]) < 12:
            report["examples"].append({"kind": kind, **details})

    full = factory()
    try:
        full.build(chunks)
        for principal in fixture["principals"]:
            visible = [c for c in chunks if allowed(c, principal)]
            readable = {c["id"] for c in visible}
            isolated = factory()
            isolated.build(visible)
            for chunk in chunks:
                for k in fixture.get("ks", [1, 3, 20]):
                    report["probes"] += 1
                    try:
                        got = observe(full, chunk["text"], principal, k)
                        want = observe(isolated, chunk["text"], principal, k)
                        forbidden = [cid for cid, _ in got if cid not in readable]
                        if forbidden:
                            fail(
                                "visibility_failures",
                                principal=principal,
                                chunk=chunk["id"],
                                k=k,
                                forbidden=forbidden,
                            )
                        if got != want:
                            fail(
                                "isolation_failures",
                                principal=principal,
                                chunk=chunk["id"],
                                k=k,
                                got=got,
                                want=want,
                            )
                        if chunk["id"] in readable and k == max(fixture.get("ks", [1, 3, 20])):
                            report["positive_controls"] += 1
                            if chunk["id"] not in {cid for cid, _ in got}:
                                fail("recall_misses", principal=principal, chunk=chunk["id"])
                    except Exception as exc:
                        fail("errors", principal=principal, chunk=chunk["id"], error=type(exc).__name__)
    except Exception as exc:
        fail("errors", stage="build", error=type(exc).__name__)

    # A source ACL may be rejected at ingest or stored as deny-all. Both are safe.
    for invalid in fixture.get("invalid_chunks", []):
        report["invalid_acl_controls"] += 1
        adapter = factory()
        try:
            adapter.build([invalid])
        except (TypeError, ValueError):
            continue
        except Exception as exc:
            fail("errors", stage="invalid-build", error=type(exc).__name__)
            continue
        for principal in fixture["principals"]:
            try:
                if observe(adapter, invalid["text"], principal, 20):
                    fail("invalid_acl_failures", chunk=invalid["id"], principal=principal)
            except Exception as exc:
                fail("errors", stage="invalid-read", error=type(exc).__name__)

    for control in fixture.get("revocations", []):
        report["revocation_controls"] += 1
        adapter = factory()
        try:
            adapter.build(chunks)
            principal = control["principal"]
            target = next(c for c in chunks if c["id"] == control["id"])
            before = observe(adapter, target["text"], principal, 20)
            if target["id"] not in {cid for cid, _ in before}:
                fail("revocation_failures", chunk=target["id"], stage="warmup-missing")
            adapter.replace_acl(target["id"], control["levels"])
            after = observe(adapter, target["text"], principal, 20)
            if target["id"] in {cid for cid, _ in after}:
                fail("revocation_failures", chunk=target["id"], stage="still-visible")
        except Exception as exc:
            fail("errors", stage="revocation", error=type(exc).__name__)
    report["passed"] = bool(report["positive_controls"]) and not any(
        report[k]
        for k in (
            "visibility_failures",
            "isolation_failures",
            "recall_misses",
            "errors",
            "invalid_acl_failures",
            "revocation_failures",
        )
    )
    return report
