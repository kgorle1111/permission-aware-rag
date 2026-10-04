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
from .store import permission_transaction
from .vectorstore import search, search_unfiltered_count

log = logging.getLogger("permrag")
EMPTY_RESPONSE = {"results": [], "answer": "No results found."}
_cache = OrderedDict()
_cache_lock = RLock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def retrieve(query: str, principal: Principal, k: int = None) -> dict:
    k = config.TOP_K if k is None else k
    try:
        if not 1 <= k <= 20:
            raise ValueError("k must be between 1 and 20")
        with permission_transaction() as (session, state):
            if state.pending:
                raise RuntimeError("permission reconciliation required")
            key = (state.revision, principal.scope_key, query, k)
            with _cache_lock:
                cached = _cache.get(key)
            if cached and time.monotonic() - cached[0] <= config.CACHE_TTL_S:
                created, response, chunk_ids, doc_ids, denied = cached
            else:
                created = time.monotonic()
                qvec = embed_one(query)
                hits = search(qvec, principal.principals, top_k=k)
                denied = search_unfiltered_count(qvec, principal.principals, top_k=k)
                results = [{"doc_id": h["doc_id"], "text": h["text"],
                            "score": round(h["score"], 4)} for h in hits]
                response = {"results": results, "answer": answer(query, results)}
                chunk_ids = [h["chunk_id"] for h in hits]
                doc_ids = sorted({h["doc_id"] for h in hits})
            write_audit(principal, query, chunk_ids, doc_ids, denied, False,
                        session=session)
        # Reaching here confirms the audit transaction committed. Copies keep
        # callers from modifying cached results. Revisions invalidate all workers.
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
