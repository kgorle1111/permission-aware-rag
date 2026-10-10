"""Circuit breakers and the ACL-safe keyword fallback.

Degradation covers INFRASTRUCTURE failures only (embedder, vector store, LLM).
Permission uncertainty (pending sync, rebuild required, fingerprint mismatch)
never degrades: retrieval fails closed before any fallback runs.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable

from . import config
from .embeddings import _STOP
from .store import ChunkACL, ChunkPolicy, ChunkText


class BreakerOpen(Exception):
    pass


class CircuitBreaker:
    """closed -> open after `threshold` consecutive failures -> half_open after
    `reset_s` (a trial call decides: success closes, failure reopens)."""

    def __init__(self, name: str, threshold: int, reset_s: float,
                 clock: Callable[[], float] = time.monotonic):
        self.name, self.threshold, self.reset_s, self.clock = name, threshold, reset_s, clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self._opened_at >= self.reset_s else "open"

    def call(self, fn, *args, **kwargs):
        if self.state == "open":
            raise BreakerOpen(f"{self.name} circuit open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            with self._lock:
                self._failures += 1
                if self._opened_at is not None or self._failures >= self.threshold:
                    self._opened_at = self.clock()
            raise
        with self._lock:
            self._failures, self._opened_at = 0, None
        return result


def _make_breakers() -> dict[str, CircuitBreaker]:
    return {n: CircuitBreaker(n, config.BREAKER_THRESHOLD, config.BREAKER_RESET_S)
            for n in ("embedder", "vectorstore", "llm")}


BREAKERS = _make_breakers()


def reset_breakers():
    BREAKERS.update(_make_breakers())


def _terms(text: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", text.lower()).split() if w not in _STOP}


def keyword_search(session, query: str, principals: list[str], k: int) -> list[dict]:
    """Term-overlap search over the SQL-mirrored chunks.

    Same rule as the vector filter: a chunk is visible only if the caller
    matches EVERY level (document, section, paragraph). A missing level denies.
    """
    terms = _terms(query)
    if not terms:
        return []
    granted = set(principals)
    rows = (session.query(ChunkText, ChunkACL, ChunkPolicy)
            .join(ChunkACL, ChunkACL.chunk_id == ChunkText.chunk_id)
            .join(ChunkPolicy, ChunkPolicy.chunk_id == ChunkText.chunk_id).all())
    hits = []
    for body, acl, policy in rows:
        levels = (policy.doc_acl, policy.section_acl, policy.paragraph_acl)
        if not all(isinstance(lv, list) and granted & set(lv) for lv in levels):
            continue
        overlap = len(terms & _terms(body.text))
        if overlap:
            hits.append({"chunk_id": body.chunk_id, "doc_id": acl.doc_id, "text": body.text,
                         "score": overlap / len(terms)})
    hits.sort(key=lambda h: (-h["score"], h["chunk_id"]))
    return hits[:k]
