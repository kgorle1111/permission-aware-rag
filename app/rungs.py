"""Tier 3 retrieval rungs (ROADMAP 8.5, 8.6, 9.2, 9.3, 9.4, 9.7), each a PermissionRAG subclass.

Every rung ranks only caller-visible chunks (it goes through `_search`, which pre-filters) and is
registered in evals/ladder.py, so a rung that leaks is rejected no matter how well it retrieves.
LLM steps (variant generation, query classification/enhancement) are injected callables; the
defaults are deterministic and need no model. Enhancers receive the query string and nothing else.
"""

import hashlib
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable

from embedding import embed
from permission_rag import PermissionRAG, normalize_acl, tokenize

_STOP = frozenset(
    "a an and are as at be by for from how i in is it of on or the to was what when where which who why with".split()
)
_STAR = frozenset({"*"})
POOL = 20  # per-variant candidates fed to fusion


# ---- 8.6 embedding cache -------------------------------------------------------------------


class EmbeddingCache:
    """Memoize an embedder, keyed by embedder id + sha256(text) so a model swap can never hit."""

    def __init__(
        self, embedder=embed, embedder_id: str = "feature-hash-256-v1", store=None, max_entries=4096
    ):
        self.embedder, self.embedder_id, self.max_entries = embedder, embedder_id, max_entries
        self.store: OrderedDict = store if store is not None else OrderedDict()
        self.hits = self.misses = 0
        self._lock = threading.Lock()

    def __call__(self, text: str) -> list[float]:
        key = f"{self.embedder_id}:{hashlib.sha256(text.encode()).hexdigest()}"
        with self._lock:
            if key in self.store:
                self.hits += 1
                return list(self.store[key])
        vec = list(self.embedder(text))
        with self._lock:
            self.misses += 1
            self.store[key] = vec
            while len(self.store) > self.max_entries:
                self.store.popitem(last=False)
        return list(vec)


# ---- fusion helpers ------------------------------------------------------------------------


def rrf_fuse(rankings: Iterable[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: score(id) = sum over rankings of 1 / (k + rank), rank from 1."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)  # stable: ties keep first-seen order


def lexical_variants(query: str) -> list[str]:
    """Deterministic no-LLM variants: original, content words only, the longest half of them.

    Short queries can yield duplicates; kept so the variant count is always 3 (a duplicate only
    doubles that list's RRF weight, never changes the order).
    """
    content = [t for t in tokenize(query) if t not in _STOP]
    longest = sorted(content, key=len, reverse=True)[: max(1, (len(content) + 1) // 2)]
    return [query, " ".join(content), " ".join(longest)]


# ---- 8.5 scoped semantic cache -------------------------------------------------------------


class SemanticCacheRAG(PermissionRAG):
    """Result cache keyed on principal scope + corpus revision + k + query embedding (cosine) + TTL."""

    # kn: per-process cache; a shared store keyed by the same scope+revision once more than one process serves

    def __init__(
        self, *a, ttl=300.0, clock=time.monotonic, threshold=0.95, embedder=None, max_entries=1024, **kw
    ):
        super().__init__(*a, **kw)
        self.ttl, self.clock, self.threshold, self.max_entries = ttl, clock, threshold, max_entries
        self._embed = embedder or EmbeddingCache()
        self._revision = 0
        self._entries: list[dict] = []
        self._cache_lock = threading.Lock()
        self.cache_hits = self.cache_misses = 0

    def _bump(self) -> None:
        with self._cache_lock:
            self._revision += 1
            self._entries.clear()

    def add_document(self, *a, **kw) -> None:
        super().add_document(*a, **kw)
        self._bump()

    def remove_document(self, doc_id: str) -> int:
        removed = super().remove_document(doc_id)
        self._bump()
        return removed

    @staticmethod
    def _scope(user: dict) -> tuple[str, ...]:
        return tuple(sorted([f"user:{user['id']}", *(f"group:{g}" for g in user.get("groups", ()))]))

    def _revalidate(self, user: dict, entry: dict) -> bool:
        """Fail closed if an ACL was edited in place since caching (revision only tracks add/remove)."""
        return all(self.can_read_chunk(user, c) for c in entry["chunks"])

    def _lookup(self, scope, vec, k, now):
        best, best_sim = None, self.threshold
        with self._cache_lock:
            self._entries[:] = [e for e in self._entries if now - e["at"] < self.ttl]
            for e in self._entries:
                if e["scope"] != scope or e["revision"] != self._revision or e["k"] != k:
                    continue
                # kn: linear scan over entries; ANN index if the cache holds more than ~10k entries
                sim = sum(a * b for a, b in zip(vec, e["vec"], strict=True))
                if sim >= best_sim:
                    best, best_sim = e, sim
        return best

    def retrieve(self, query: str, user: dict, k: int = 3) -> list[dict]:
        t0 = time.perf_counter()
        scope, vec, now = self._scope(user), self._embed(query), self.clock()
        entry = self._lookup(scope, vec, k, now) if any(vec) else None
        if entry is not None and self._revalidate(user, entry):
            self.cache_hits += 1
            results = [dict(r) for r in entry["results"]]
            self._record(user, results, entry["denied"], t0)
            return results
        self.cache_misses += 1
        with self._cache_lock:
            revision = self._revision
        scored, denied = self._search(query, user)
        top = [(s, c) for s, c in scored[:k] if s > 0]
        results = self._results(top, k)
        if any(vec):
            with self._cache_lock:
                if revision == self._revision:  # a concurrent add/remove invalidated this result
                    self._entries.append(
                        {
                            "scope": scope,
                            "revision": revision,
                            "k": k,
                            "vec": vec,
                            "at": now,
                            "results": [dict(r) for r in results],
                            "chunks": [c for _, c in top],
                            "denied": denied,
                        }
                    )
                    del self._entries[: -self.max_entries]
        self._record(user, results, denied, t0)
        return results


# ---- 9.2 structure-aware chunking ----------------------------------------------------------


class StructureChunkedRAG(PermissionRAG):
    """Merge only units whose (section ACL, paragraph ACL, breadcrumb) are identical; split on any change."""

    TARGET_WORDS = 450  # kn: words approximate tokens (~1.3x); use the embedder's tokenizer for real budgets
    OVERLAP = 0.15
    SCORES_DIFFER = (
        True  # chunk boundaries differ from baseline, so the oracle must be this class on the isolated corpus
    )

    def document_chunks(self, text, acl, chunk_words=80, *, sections=None):
        # chunk_words is ignored on purpose: size comes from TARGET_WORDS. Validation is the baseline's.
        PermissionRAG.document_chunks(text, acl, chunk_words, sections=sections)
        doc_acl = normalize_acl(acl)
        units = [(_STAR, _STAR, "", text)]
        for section in sections or []:
            s_acl = normalize_acl(section["acl"]) if "acl" in section else _STAR
            crumb = section["title"] if isinstance(section.get("title"), str) else ""
            if "paragraphs" in section:
                for para in section["paragraphs"]:
                    p_acl = normalize_acl(para["acl"]) if "acl" in para else _STAR
                    units.append((s_acl, p_acl, crumb, para["text"]))
            else:
                units.append((s_acl, _STAR, crumb, section["text"]))
        groups: list[list] = []
        for s_acl, p_acl, crumb, body in units:
            if not body.strip():
                continue
            if groups and groups[-1][:3] == [s_acl, p_acl, crumb]:
                groups[-1][3].append(body)
            else:
                groups.append([s_acl, p_acl, crumb, [body]])
        chunks = []
        for s_acl, p_acl, crumb, bodies in groups:
            for part in self._pack(" ".join(bodies)):
                chunks.append(
                    {
                        "text": f"{crumb} > {part}" if crumb else part,
                        "acl": doc_acl,
                        "acl_doc": doc_acl,
                        "acl_section": s_acl,
                        "acl_para": p_acl,
                    }
                )
        return chunks

    def _pack(self, body: str) -> list[str]:
        sentences = []
        for s in re.split(r"(?<=[.!?])\s+", body.strip()):
            words = s.split()
            sentences += [
                " ".join(words[i : i + self.TARGET_WORDS]) for i in range(0, len(words), self.TARGET_WORDS)
            ]
        out, cur, n = [], [], 0
        budget = int(self.TARGET_WORDS * self.OVERLAP)
        for s in sentences:
            w = len(s.split())
            if cur and n + w > self.TARGET_WORDS:
                out.append(" ".join(cur))
                keep, kept = [], 0
                for prev in reversed(cur):
                    pw = len(prev.split())
                    if kept + pw > budget:
                        break
                    keep.insert(0, prev)
                    kept += pw
                cur, n = keep, kept
            cur.append(s)
            n += w
        if cur:
            out.append(" ".join(cur))
        return out


# ---- 9.4 multi-query + RRF -----------------------------------------------------------------


class MultiQueryRRF(PermissionRAG):
    """Retrieve each query variant under the SAME principal, fuse visible results with RRF (k=60)."""

    SCORES_DIFFER = True
    SCORE_DIGITS = 6
    MAX_VARIANTS = 8

    def __init__(self, *a, variants: Callable[[str], list[str]] = lexical_variants, rrf_k: int = 60, **kw):
        super().__init__(*a, **kw)
        self.variants, self.rrf_k = variants, rrf_k

    def _variants(self, query: str) -> list[str]:
        try:
            got = [v for v in self.variants(query) if isinstance(v, str)][: self.MAX_VARIANTS]
        except Exception:  # an injected generator failing must degrade to plain retrieval, not an error
            got = []
        return got or [query]

    def _search(self, query, user):
        rankings, by_id, denied = [], {}, 0
        for variant in self._variants(query):
            scored, denied = super()._search(variant, user)
            top = [c for s, c in scored[:POOL] if s > 0]
            by_id.update({c["id"]: c for c in top})
            rankings.append([c["id"] for c in top])
        return [(score, by_id[i]) for i, score in rrf_fuse(rankings, self.rrf_k)], denied


# ---- 9.3 query router + HyDE / rewrite / step-back -----------------------------------------

MODES = ("rewrite", "hyde", "step_back")  # anything else (none, factual_precise, junk) skips enhancement
MAX_ENHANCED_CHARS = 2000


def fake_classifier(query: str) -> str:
    """Deterministic stand-in for an LLM router: exact lookups skip, long queries step back."""
    if any(ch.isdigit() for ch in query):
        return "factual_precise"
    return "step_back" if len(tokenize(query)) >= 8 else "rewrite"


def fake_enhancer(mode: str, query: str) -> str:
    """Deterministic stand-in for an LLM rewrite/HyDE/step-back call. Sees only (mode, query)."""
    variants = lexical_variants(query)
    return variants[2] if mode == "step_back" else variants[1]


class RoutedRAG(PermissionRAG):
    """Classify the query; for rewrite/hyde/step_back merge enhanced-query results with the baseline's.

    The enhancer gets (mode, query) only: never documents, ACLs or the corpus. Its output is
    treated as untrusted input to the same pre-filtered retriever, so it cannot widen access.
    """

    def __init__(self, *a, classifier=None, enhancer=None, rrf_k: int = 60, **kw):
        super().__init__(*a, **kw)
        self.classifier = classifier or (lambda q: "none")
        self.enhancer, self.rrf_k = enhancer, rrf_k

    def _enhanced_query(self, query: str) -> str | None:
        try:
            mode = self.classifier(query)
            if mode not in MODES or self.enhancer is None:
                return None
            enhanced = self.enhancer(mode, query)
        except Exception:  # classifier/enhancer outage degrades to baseline retrieval
            return None
        if not isinstance(enhanced, str) or not enhanced.strip() or len(enhanced) > MAX_ENHANCED_CHARS:
            return None
        return enhanced

    def _search(self, query, user):
        base, denied = super()._search(query, user)
        enhanced = self._enhanced_query(query)
        if enhanced is None:
            return base, denied
        extra, _ = super()._search(enhanced, user)
        tops = [[c for s, c in lst[:POOL] if s > 0] for lst in (base, extra)]
        by_id = {c["id"]: c for top in tops for c in top}
        fused = rrf_fuse([[c["id"] for c in top] for top in tops], self.rrf_k)
        return [(score, by_id[i]) for i, score in fused], denied


class RoutedRAGFake(RoutedRAG):
    """Ladder entry: router wired to the deterministic fakes (no real LLM was measured)."""

    SCORES_DIFFER = True

    def __init__(self, *a, **kw):
        super().__init__(*a, classifier=fake_classifier, enhancer=fake_enhancer, **kw)


# ---- 9.7 abstain ---------------------------------------------------------------------------

ABSTAIN_MESSAGE = "insufficient information"


class AbstainRAG(PermissionRAG):
    """Return nothing (plus a flag) when the best VISIBLE score is below the threshold."""

    DEFAULT_THRESHOLD = 1.0  # chosen a priori (about one rare-term match), not tuned on the eval set
    SCORES_DIFFER = True

    def __init__(self, *a, threshold: float = DEFAULT_THRESHOLD, **kw):
        super().__init__(*a, **kw)
        self.threshold = threshold

    def retrieve_with_status(self, query: str, user: dict, k: int = 3) -> dict:
        t0 = time.perf_counter()
        scored, denied = self._search(query, user)
        top = round(scored[0][0], self.SCORE_DIGITS) if scored else 0.0  # compare what callers would see
        abstained = top < self.threshold
        results = [] if abstained else self._results(scored, k)
        self._record(user, results, denied, t0)
        out = {"results": results, "abstained": abstained}
        return {**out, "message": ABSTAIN_MESSAGE} if abstained else out

    def retrieve(self, query: str, user: dict, k: int = 3) -> list[dict]:
        return self.retrieve_with_status(query, user, k)["results"]


RUNGS = [SemanticCacheRAG, StructureChunkedRAG, MultiQueryRRF, RoutedRAGFake, AbstainRAG]
