"""Known-leaky retrievers. A leak gate is only evidence if it fails on these.

Each mutant is one realistic ACL bug. `run_evals.py --mutants` must catch every one;
a mutant that survives means the gate is too weak, not that the mutant is harmless.
"""

from collections import Counter

from permission_rag import PermissionRAG, tokenize
from rungs import SemanticCacheRAG


def _rank(rag, query, candidates, stats_over, k):
    """BM25 with corpus statistics taken from `stats_over` instead of the visible set."""
    df = Counter()
    for c in stats_over:
        df.update(set(c["tf"]))
    n = len(stats_over)
    avglen = sum(sum(c["tf"].values()) for c in stats_over) / n if n else 1.0
    q = set(tokenize(query))
    scored = sorted(
        ((rag._score(q, c, df, n, avglen), c) for c in candidates), key=lambda sc: sc[0], reverse=True
    )
    return [
        {"id": c["id"], "doc_id": c["doc_id"], "text": c["text"], "score": round(s, 4)}
        for s, c in scored[:k]
        if s > 0
    ]


class NoFilter(PermissionRAG):
    """Forgot the ACL check entirely."""

    @staticmethod
    def can_read(user, acl):
        return True


class GlobalIdf(PermissionRAG):
    """S1: filters correctly but computes IDF over every chunk, so hidden docs move scores."""

    def retrieve(self, query, user, k=3):
        visible = [c for c in self.chunks if self.can_read(user, c["acl"])]
        return _rank(self, query, visible, self.chunks, k)


class PostFilterTopK(PermissionRAG):
    """Rank everything, take top-k, then drop what the user can't read."""

    def retrieve(self, query, user, k=3):
        top = _rank(self, query, self.chunks, self.chunks, k)
        acl = {c["id"]: c["acl"] for c in self.chunks}
        return [r for r in top if self.can_read(user, acl[r["id"]])]


class StarSubstring(PermissionRAG):
    """Public check written as a substring test: 'group:banking*' reads as public."""

    @staticmethod
    def can_read(user, acl):
        if any("*" in e for e in acl):
            return True
        return PermissionRAG.can_read(user, acl)


class GroupPrefix(PermissionRAG):
    """Group match by prefix: member of 'underwriting' reads 'group:underwriting-admin'."""

    @staticmethod
    def can_read(user, acl):
        if f"user:{user['id']}" in acl or "*" in acl:
            return True
        return any(e.startswith(f"group:{g}") for g in user.get("groups", ()) for e in acl)


class SharedCache(PermissionRAG):
    """Result cache keyed on (query, k) — forgot the principal in the key."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._cache = {}

    def retrieve(self, query, user, k=3):
        if (query, k) not in self._cache:
            self._cache[(query, k)] = super().retrieve(query, user, k)
        return self._cache[(query, k)]


MUTANTS = [NoFilter, GlobalIdf, PostFilterTopK, StarSubstring, GroupPrefix, SharedCache]


class AnyLevelGrants(PermissionRAG):
    """Wrongly treats document/section/paragraph grants as alternatives."""

    def can_read_chunk(self, user, chunk):
        return any(self.can_read(user, chunk[level]) for level in ("acl_doc", "acl_section", "acl_para"))


class FlattenIntersection(PermissionRAG):
    """Locks out users who satisfy levels through different memberships."""

    def can_read_chunk(self, user, chunk):
        levels = [chunk[level] for level in ("acl_doc", "acl_section", "acl_para") if "*" not in chunk[level]]
        flat = set.intersection(*(set(level) for level in levels)) if levels else {"*"}
        return self.can_read(user, flat)


MUTANTS += [AnyLevelGrants, FlattenIntersection]


class SemanticCacheNoScope(SemanticCacheRAG):
    """Semantic cache whose key forgot the principal and which never re-checks readability."""

    @staticmethod
    def _scope(user):
        return ()

    def _revalidate(self, user, entry):
        return True


MUTANTS += [SemanticCacheNoScope]
