"""The retrieval pipeline, in the only order that is safe:

    verified identity -> ACL pre-filter (inside the engine) -> rank -> audit

Invariants enforced here (and proven in tests/):
- No code path exists where an unreadable chunk meets the scorer's output.
- FAIL CLOSED: any internal error returns an empty result, never unfiltered.
- The "no results" response is byte-identical whether the content is
  forbidden or simply doesn't exist (existence indistinguishability).
- The answer cache key includes the caller's permission scope, so a
  privileged user's answer can never be served to an unprivileged one.
"""
from __future__ import annotations

import logging
import time

from . import config
from .audit import write_audit
from .embeddings import embed_one
from .identity import Principal
from .vectorstore import search, search_unfiltered_count

log = logging.getLogger("permrag")

EMPTY_RESPONSE = {"results": [], "answer": "No results found."}

# permission-scoped answer cache: key = (scope_key, normalized query)
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def clear_cache():
    _cache.clear()


def retrieve(query: str, principal: Principal, k: int = None) -> dict:
    k = k or config.TOP_K
    t0 = time.perf_counter()
    cache_key = (principal.scope_key, " ".join(query.lower().split()))

    cached = _cache.get(cache_key)
    if cached and time.perf_counter() - cached[0] <= config.CACHE_TTL_S:
        _, response, ainfo = cached
        try:  # a cache hit is still a retrieval — it gets an audit row too
            write_audit(principal, query, ainfo["chunk_ids"], ainfo["doc_ids"],
                        denied_count=ainfo["denied"], fail_closed=False)
        except Exception:
            log.exception("audit write failed on cache hit")
        return response

    try:
        qvec = embed_one(query)
        hits = search(qvec, principal.principals, top_k=k)
        denied = search_unfiltered_count(qvec, principal.principals, top_k=k)
    except Exception:
        # FAIL CLOSED: if the permission layer is unsure, the answer is nothing.
        log.exception("retrieval error — failing closed")
        try:
            write_audit(principal, query, [], [], denied_count=0, fail_closed=True)
        except Exception:
            log.exception("audit write failed during fail-closed path")
        return dict(EMPTY_RESPONSE)

    results = [
        {"doc_id": h["doc_id"], "text": h["text"], "score": round(h["score"], 4)}
        for h in hits
    ]
    response = {
        "results": results,
        "answer": None,  # filled by generation.py from PERMITTED chunks only
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    if not results:
        # identical body whether the topic is forbidden or nonexistent
        response = {**dict(EMPTY_RESPONSE),
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    chunk_ids = [h["chunk_id"] for h in hits]
    doc_ids = sorted({h["doc_id"] for h in hits})
    try:
        write_audit(principal, query, chunk_ids, doc_ids,
                    denied_count=denied, fail_closed=False)
    except Exception:
        # a lost audit row must not take down retrieval, but it is loudly logged
        log.exception("audit write failed")

    _cache[cache_key] = (time.perf_counter(), response,
                         {"chunk_ids": chunk_ids, "doc_ids": doc_ids, "denied": denied})
    return response
