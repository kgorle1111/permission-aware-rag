"""Filtered retrieval; serialized permission check, generation, and durable audit."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import logging
import time
from threading import RLock

from . import config
from .audit import write_audit
from .embeddings import embed_one
from .generation import answer
from .identity import Principal
from .redact import redact
from .resilience import BREAKERS, keyword_search
from .store import fingerprint_problem, permission_transaction
from .vectorstore import search, search_unfiltered_count

log = logging.getLogger("permrag")
# Fail-closed and no-match responses are deliberately identical: a denied or broken
# request must not be distinguishable from a query about nothing.
EMPTY_RESPONSE = {"results": [], "answer": "No results found.", "degraded": False, "source": "full_rag"}
_cache = OrderedDict()
_cache_lock = RLock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


UNAVAILABLE_ANSWER = "Service temporarily unavailable. Please retry shortly."


def _extractive(results: list[dict]) -> str:
    if not results:
        return "No results found."
    return f"{results[0]['text']} [{results[0]['doc_id']}]"


def _vector_hits(qvec, principals, k):
    return (search(qvec, principals, top_k=k),
            search_unfiltered_count(qvec, principals, top_k=k))


def _compute(session, query: str, principal: Principal, k: int):
    """Full RAG, degrading on dependency failure. Every level is ACL-filtered
    (vector filter or keyword_search); permission state was checked by the caller."""
    degraded, source = False, "full_rag"
    try:
        qvec = BREAKERS["embedder"].call(embed_one, query)
        hits, denied = BREAKERS["vectorstore"].call(_vector_hits, qvec, principal.principals, k)
    except Exception as exc:
        log.warning("dependency unavailable (%s); using keyword fallback", type(exc).__name__)
        try:
            hits, denied = keyword_search(session, query, principal.principals, k), 0
        except Exception:
            log.exception("keyword fallback failed")
            return ({"results": [], "answer": UNAVAILABLE_ANSWER,
                     "degraded": True, "source": "unavailable"}, [], [], 0)
        degraded, source = True, "keyword_fallback"
    results = [{"doc_id": h["doc_id"], "text": redact(h["text"]),
                "score": round(h["score"], 4)} for h in hits]
    if source == "keyword_fallback":
        text = _extractive(results)
    else:
        try:
            text = BREAKERS["llm"].call(answer, query, results)
        except Exception as exc:
            log.warning("generation unavailable (%s); retrieval-only answer", type(exc).__name__)
            degraded, source, text = True, "retrieval_only", _extractive(results)
    response = {"results": results, "answer": redact(text), "degraded": degraded, "source": source}
    return (response, [h["chunk_id"] for h in hits], sorted({h["doc_id"] for h in hits}), denied)


def retrieve(query: str, principal: Principal, k: int = None) -> dict:
    k = config.TOP_K if k is None else k
    try:
        if not 1 <= k <= 20:
            raise ValueError("k must be between 1 and 20")
        with permission_transaction() as (session, state):
            if state.pending or state.rebuild_required:
                raise RuntimeError("permission reconciliation required")
            key = (state.revision, principal.scope_key, query, k)
            with _cache_lock:
                cached = _cache.get(key)
            if cached and time.monotonic() - cached[0] <= config.CACHE_TTL_S:
                created, response, chunk_ids, doc_ids, denied = cached
            else:
                created = time.monotonic()
                problem = fingerprint_problem(session)
                if problem:
                    raise RuntimeError(problem)
                response, chunk_ids, doc_ids, denied = _compute(session, query, principal, k)
            write_audit(principal, query, chunk_ids, doc_ids, denied,
                        response["source"] == "unavailable", session=session)
        # Reaching here confirms the audit transaction committed. Copies keep
        # callers from modifying cached results. Revisions invalidate all workers.
        if response["degraded"]:
            return deepcopy(response)  # never cache a degraded answer past recovery
        with _cache_lock:
            _cache[key] = (created, deepcopy(response), chunk_ids, doc_ids, denied)
            _cache.move_to_end(key)
            while len(_cache) > max(0, config.CACHE_MAX_ENTRIES):
                _cache.popitem(last=False)
        return deepcopy(response)
    except Exception:
        log.exception("retrieval failed closed")
        try:
            write_audit(principal, query, [], [], 0, True)
        except Exception:
            log.exception("fail-closed audit unavailable")
        return deepcopy(EMPTY_RESPONSE)
