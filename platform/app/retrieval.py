"""Filtered retrieval; serialized permission check, generation, and durable audit."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import logging
import time
from time import perf_counter
from threading import RLock

from . import config, generation
from . import observability as obs
from .audit import write_audit
from .embeddings import embed_one
from .generation import Generated, answer
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


BUDGET_NOTE = "Daily LLM budget reached; showing retrieval only."


def _ms(since: float) -> float:
    return round((perf_counter() - since) * 1000, 1)


def _generate(query: str, results: list[dict]):
    """`answer` swallows provider errors into an extractive fallback; re-raise that here so the
    LLM breaker sees real outages instead of counting every fallback as a success."""
    text = answer(query, results)
    if isinstance(text, Generated) and text.failed:
        raise RuntimeError("generation provider failed")
    return text


def _compute(session, query: str, principal: Principal, k: int, rec: dict):
    """Full RAG, degrading on dependency failure. Every level is ACL-filtered
    (vector filter or keyword_search); permission state was checked by the caller."""
    degraded, source, note = False, "full_rag", None
    started = perf_counter()
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
    rec["retrieve_ms"] = _ms(started)
    results = [{"doc_id": h["doc_id"], "text": redact(h["text"]),
                "score": round(h["score"], 4)} for h in hits]
    if source == "keyword_fallback":
        text = _extractive(results)
    elif (results and config.ANTHROPIC_API_KEY
          and obs.OPS.would_exceed(generation.projected_cost(query, results))):
        degraded, source, note = True, "retrieval_only", BUDGET_NOTE
        text = _extractive(results)
    else:
        started = perf_counter()
        try:
            text = BREAKERS["llm"].call(_generate, query, results)
            usage = text.usage if isinstance(text, Generated) else {}
            rec["tokens_in"] = obs.clean_usage(usage)["input_tokens"]
            rec["tokens_out"] = obs.clean_usage(usage)["output_tokens"]
            rec["tokens_cached"] = obs.clean_usage(usage)["cache_read_input_tokens"]
            rec["est_cost_usd"] = obs.est_cost(usage)
            rec["unverified_citations"] = obs.unverified_citations(
                text, {r["doc_id"] for r in results})
        except Exception as exc:
            log.warning("generation unavailable (%s); retrieval-only answer", type(exc).__name__)
            degraded, source, text = True, "retrieval_only", _extractive(results)
        rec["llm_ms"] = _ms(started)
    response = {"results": results, "answer": redact(text), "degraded": degraded, "source": source}
    if note:
        response["note"] = note
    return (response, [h["chunk_id"] for h in hits], sorted({h["doc_id"] for h in hits}), denied)


def _outcome(response: dict) -> str:
    return {"unavailable": "failed_closed", "keyword_fallback": "degraded",
            "retrieval_only": "llm_fallback"}.get(
        response["source"], "ok" if response["results"] else "no_results")


def retrieve(query: str, principal: Principal, k: int = None, request_id: str = None) -> dict:
    rec, started = obs.new_record(request_id), perf_counter()
    response, failed = _retrieve(query, principal, k, rec)
    rec.update(source=response["source"], degraded=response["degraded"],
               returned=len(response["results"]))
    if failed:  # the caller sees the canonical empty response; the log keeps the truth
        rec.update(outcome="failed_closed", source="unavailable", degraded=True)
    else:
        rec["outcome"] = _outcome(response)
    rec["total_ms"] = _ms(started)
    obs.OPS.record(rec)
    obs.emit(rec)
    return response


def _retrieve(query: str, principal: Principal, k: int, rec: dict):
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
                response, chunk_ids, doc_ids, denied = _compute(session, query, principal, k, rec)
            rec["denied"] = denied
            write_audit(principal, query, chunk_ids, doc_ids, denied,
                        response["source"] == "unavailable", session=session)
        # Reaching here confirms the audit transaction committed. Copies keep
        # callers from modifying cached results. Revisions invalidate all workers.
        if response["degraded"]:
            return deepcopy(response), False  # never cache a degraded answer past recovery
        with _cache_lock:
            _cache[key] = (created, deepcopy(response), chunk_ids, doc_ids, denied)
            _cache.move_to_end(key)
            while len(_cache) > max(0, config.CACHE_MAX_ENTRIES):
                _cache.popitem(last=False)
        return deepcopy(response), False
    except Exception:
        log.exception("retrieval failed closed")
        try:
            write_audit(principal, query, [], [], 0, True)
        except Exception:
            log.exception("fail-closed audit unavailable")
        return deepcopy(EMPTY_RESPONSE), True
