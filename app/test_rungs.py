"""Tier 3 retrieval rungs: each must add its feature without ever widening visibility."""

import random

import pytest
import rungs
from mutants import MUTANTS, SemanticCacheNoScope
from permission_rag import PermissionRAG
from rungs import AbstainRAG, EmbeddingCache, MultiQueryRRF, RoutedRAG, SemanticCacheRAG, StructureChunkedRAG

EXEC = {"id": "e1", "groups": ["exec"]}
STAFF = {"id": "s1", "groups": ["staff"]}
SECRET = "zephyr budget layoff plan"
PUBLIC = "public budget overview for all staff"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def seeded(cls, **kw):
    rag = cls(**kw)
    rag.add_document("secret", SECRET, ["group:exec"])
    rag.add_document("public", PUBLIC, ["*"])
    return rag


def doc_ids(results):
    return {r["doc_id"] for r in results}


# ---- 8.6 embedding cache -------------------------------------------------------------------


def test_embedding_cache_memoizes_identical_text():
    calls = []

    def fake(text):
        calls.append(text)
        return [float(len(text))]

    cache = EmbeddingCache(fake, "fake-v1")
    assert cache("hello") == cache("hello") == [5.0]
    assert calls == ["hello"]
    assert (cache.hits, cache.misses) == (1, 1)


def test_embedding_cache_different_embedder_id_does_not_hit():
    store: dict = {}
    calls = []
    a = EmbeddingCache(lambda t: calls.append("a") or [1.0], "model-a", store=store)
    b = EmbeddingCache(lambda t: calls.append("b") or [2.0], "model-b", store=store)
    assert a("same text") == [1.0]
    assert b("same text") == [2.0]
    assert calls == ["a", "b"]


def test_embedding_cache_returns_copies():
    cache = EmbeddingCache(lambda t: [1.0, 2.0], "m")
    cache("x").append(99.0)
    assert cache("x") == [1.0, 2.0]


# ---- 8.5 scoped semantic cache -------------------------------------------------------------


def test_cache_hit_returns_identical_results():
    rag = seeded(SemanticCacheRAG)
    first = rag.retrieve("budget plan", EXEC, k=3)
    second = rag.retrieve("budget plan", EXEC, k=3)
    assert first and second == first
    assert (rag.cache_hits, rag.cache_misses) == (1, 1)
    second[0]["text"] = "mutated"
    assert rag.retrieve("budget plan", EXEC, k=3) == first


def test_cache_never_serves_across_scopes_even_at_similarity_one():
    rag = seeded(SemanticCacheRAG)
    assert "secret" in doc_ids(rag.retrieve("budget plan", EXEC, k=3))
    assert doc_ids(rag.retrieve("budget plan", STAFF, k=3)) == {"public"}
    assert rag.cache_hits == 0


def test_scope_key_alone_blocks_cross_scope_hits_without_revalidation(monkeypatch):
    rag = seeded(SemanticCacheRAG)
    monkeypatch.setattr(rag, "_revalidate", lambda user, entry: True)
    rag.retrieve("budget plan", EXEC, k=3)
    assert doc_ids(rag.retrieve("budget plan", STAFF, k=3)) == {"public"}
    assert rag.cache_hits == 0


def test_entry_from_older_revision_is_never_served():
    rag = seeded(SemanticCacheRAG)
    rag.retrieve("budget plan", EXEC, k=3)
    rag._revision += 1  # simulate a bump that raced with the insert
    rag.retrieve("budget plan", EXEC, k=3)
    assert rag.cache_hits == 0


def test_scope_is_sorted_principals_so_group_order_does_not_matter():
    rag = seeded(SemanticCacheRAG)
    rag.retrieve("budget plan", {"id": "u", "groups": ["exec", "staff"]}, k=3)
    rag.retrieve("budget plan", {"id": "u", "groups": ["staff", "exec"]}, k=3)
    assert rag.cache_hits == 1
    rag.retrieve("budget plan", {"id": "u", "groups": ["staff"]}, k=3)
    assert rag.cache_hits == 1


def test_negative_control_leaks_across_scopes():
    rag = seeded(SemanticCacheNoScope)
    rag.retrieve("budget plan", EXEC, k=3)
    assert "secret" in doc_ids(rag.retrieve("budget plan", STAFF, k=3))
    assert SemanticCacheNoScope in MUTANTS


def test_revision_bump_invalidates_on_add_and_remove():
    rag = seeded(SemanticCacheRAG)
    assert doc_ids(rag.retrieve("budget plan", EXEC, k=5)) == {"secret", "public"}
    rag.add_document("extra", "budget plan addendum", ["*"])
    assert "extra" in doc_ids(rag.retrieve("budget plan", EXEC, k=5))
    assert rag.cache_hits == 0
    rag.remove_document("secret")
    assert "secret" not in doc_ids(rag.retrieve("budget plan", EXEC, k=5))
    assert rag.cache_hits == 0


def test_ttl_expiry():
    clock = Clock()
    rag = seeded(SemanticCacheRAG, ttl=60.0, clock=clock)
    rag.retrieve("budget plan", EXEC, k=3)
    clock.t += 59.9
    rag.retrieve("budget plan", EXEC, k=3)
    assert rag.cache_hits == 1
    clock.t += 60.1
    rag.retrieve("budget plan", EXEC, k=3)
    assert (rag.cache_hits, rag.cache_misses) == (1, 2)


def test_in_place_acl_revocation_is_not_served_from_cache():
    rag = seeded(SemanticCacheRAG)
    assert "secret" in doc_ids(rag.retrieve("budget plan", EXEC, k=3))
    for c in rag.chunks:
        if c["doc_id"] == "secret":
            c["acl_doc"] = c["acl"] = frozenset({"group:nobody"})
    assert "secret" not in doc_ids(rag.retrieve("budget plan", EXEC, k=3))


def test_hit_still_writes_audit_and_different_k_or_query_misses():
    rag = seeded(SemanticCacheRAG)
    rag.retrieve("budget plan", EXEC, k=3)
    rag.retrieve("budget plan", EXEC, k=3)
    assert len(rag.audit) == 2 and rag.audit[1]["returned"] == rag.audit[0]["returned"]
    rag.retrieve("budget plan", EXEC, k=1)
    rag.retrieve("completely unrelated words here", EXEC, k=3)
    assert rag.cache_hits == 1


# ---- 9.2 structure-aware chunking ----------------------------------------------------------


def _sentences(prefix: str, n: int, words: int = 30) -> str:
    return " ".join(f"{prefix}{i} " + " ".join(["filler"] * (words - 2)) + "." for i in range(n))


def test_mixed_acl_boundary_always_splits():
    rag = StructureChunkedRAG()
    rag.add_document(
        "d",
        "doc intro alpha.",
        ["*"],
        sections=[{"text": "public beta text."}, {"text": "restricted gamma text.", "acl": ["group:exec"]}],
    )
    texts = [c["text"] for c in rag.chunks]
    assert not any("beta" in t and "gamma" in t for t in texts)
    gamma = next(c for c in rag.chunks if "gamma" in c["text"])
    assert gamma["acl_section"] == frozenset({"group:exec"})
    assert "alpha" not in gamma["text"] and "beta" not in gamma["text"]


def test_random_acl_layouts_never_merge_different_triples():
    rng = random.Random(7)
    levels = [["*"], ["group:a"], ["group:b"]]
    for trial in range(30):
        paras, owner = [], {}
        for i in range(rng.randint(2, 6)):
            tok = f"tok{trial}x{i}"
            owner[tok] = (rng.choice(levels), rng.choice(levels))
            paras.append({"text": f"{tok} short body.", "acl": owner[tok][1]})
        sections = [{"acl": owner[p["text"].split()[0]][0], "paragraphs": [p]} for p in paras]
        rag = StructureChunkedRAG()
        rag.add_document("d", "", ["*"], sections=sections)
        for c in rag.chunks:
            toks = {t for t in c["text"].split() if t.startswith("tok")}
            triples = {(frozenset(owner[t][0]), frozenset(owner[t][1])) for t in toks}
            assert triples == {(c["acl_section"], c["acl_para"])}


def test_restricted_paragraph_in_public_doc_stays_hidden_to_outsiders():
    rag = StructureChunkedRAG()
    rag.add_document(
        "memo",
        "",
        ["*"],
        sections=[
            {
                "title": "Compensation",
                "paragraphs": [
                    {"text": "general pay bands are published yearly."},
                    {"text": "executive bonus pool is quietly doubled.", "acl": ["group:exec"]},
                ],
            }
        ],
    )
    assert rag.retrieve("executive bonus pool", STAFF, k=5) == []
    assert doc_ids(rag.retrieve("executive bonus pool", EXEC, k=5)) == {"memo"}
    assert rag.retrieve("general pay bands", STAFF, k=5)


def test_size_overlap_and_breadcrumb():
    rag = StructureChunkedRAG()
    rag.add_document("long", "", ["*"], sections=[{"title": "Exclusions", "text": _sentences("s", 40)}])
    sizes = [len(c["text"].split()) for c in rag.chunks]
    assert len(sizes) >= 3 and max(sizes) <= 600
    assert all(c["text"].startswith("Exclusions > ") for c in rag.chunks)
    assert all(s >= 300 for s in sizes[:-1])
    first = [s.strip() + "." for s in rag.chunks[0]["text"].split(".") if s.strip()]
    second = rag.chunks[1]["text"]
    carried = [s for s in first if s in second]
    overlap_words = sum(len(s.split()) for s in carried)
    assert (
        carried
        and 0.10 * StructureChunkedRAG.TARGET_WORDS
        <= overlap_words
        <= 0.20 * StructureChunkedRAG.TARGET_WORDS
    )


def test_chunking_rejects_bad_acl_like_baseline():
    with pytest.raises(ValueError):
        StructureChunkedRAG().add_document("d", "x", [])


# ---- 9.4 multi-query + RRF -----------------------------------------------------------------


def test_rrf_math_exact_on_hand_case():
    rankings = [["a", "b", "c"], ["b", "a"], ["b"]]
    fused = dict(rungs.rrf_fuse(rankings, k=60))
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61 + 1 / 61)
    assert fused["c"] == pytest.approx(1 / 63)
    assert [d for d, _ in rungs.rrf_fuse(rankings)] == ["b", "a", "c"]


def test_default_variants_are_deterministic_and_at_least_three():
    q = "What is the status of policy 10023 for Delgado?"
    v = rungs.lexical_variants(q)
    assert len(v) >= 3 and v[0] == q
    assert v == rungs.lexical_variants(q)


def test_multiquery_same_principal_and_visible_only(monkeypatch):
    seen = []
    real = PermissionRAG._search

    def spy(self, query, user):
        seen.append(user)
        return real(self, query, user)

    monkeypatch.setattr(PermissionRAG, "_search", spy)
    rag = seeded(MultiQueryRRF, variants=lambda q: [q, "zephyr layoff", "zephyr budget", "layoff plan"])
    out = rag.retrieve("budget plan", STAFF, k=5)
    assert len(seen) == 4 and all(u is STAFF for u in seen)
    assert doc_ids(out) == {"public"}
    assert doc_ids(rag.retrieve("budget plan", EXEC, k=5)) == {"secret", "public"}
    assert len(rag.audit) == 2  # one audit entry per user query, not per variant


def test_multiquery_scores_are_rrf_of_per_variant_ranks():
    rag = MultiQueryRRF(variants=lambda q: [q, q, q])
    rag.add_document("d1", "alpha beta", ["*"])
    rag.add_document("d2", "alpha", ["*"])
    out = rag.retrieve("alpha beta", STAFF, k=2)
    assert [r["id"] for r in out] == ["d1#0", "d2#0"]
    assert out[0]["score"] == pytest.approx(3 / 61, abs=1e-6)
    assert out[1]["score"] == pytest.approx(3 / 62, abs=1e-6)


# ---- 9.3 router + HyDE / rewrite / step-back -----------------------------------------------


def test_router_default_is_none_and_matches_baseline():
    rag = seeded(RoutedRAG)
    base = seeded(PermissionRAG)
    assert rag.retrieve("budget plan", EXEC, k=3) == base.retrieve("budget plan", EXEC, k=3)


@pytest.mark.parametrize("label", ["none", "factual_precise", "bogus", None, 7])
def test_router_skips_enhancement(label):
    calls = []
    rag = seeded(RoutedRAG, classifier=lambda q: label, enhancer=lambda m, q: calls.append((m, q)) or q)
    rag.retrieve("budget plan", EXEC, k=3)
    assert calls == []


@pytest.mark.parametrize("mode", ["rewrite", "hyde", "step_back"])
def test_enhancer_sees_only_mode_and_query_and_results_are_merged(mode):
    calls = []

    def enhancer(*args, **kw):
        calls.append((args, kw))
        return "zephyr layoff"

    rag = seeded(RoutedRAG, classifier=lambda q: mode, enhancer=enhancer)
    out = rag.retrieve("budget plan", EXEC, k=5)
    assert calls == [((mode, "budget plan"), {})]
    assert SECRET not in repr(calls) and PUBLIC not in repr(calls)
    assert doc_ids(out) == {"secret", "public"}


def test_enhanced_query_cannot_widen_visibility():
    rag = seeded(RoutedRAG, classifier=lambda q: "hyde", enhancer=lambda m, q: SECRET)
    assert doc_ids(rag.retrieve("budget plan", STAFF, k=5)) == {"public"}


@pytest.mark.parametrize("bad", [None, 123, "x" * 100_000])
def test_non_string_or_oversize_enhancement_is_ignored(bad):
    rag = seeded(RoutedRAG, classifier=lambda q: "rewrite", enhancer=lambda m, q: bad)
    base = seeded(PermissionRAG).retrieve("budget plan", EXEC, k=3)
    assert rag.retrieve("budget plan", EXEC, k=3) == base


# ---- 9.7 abstain ---------------------------------------------------------------------------


def test_abstain_both_sides_of_threshold():
    top = seeded(PermissionRAG).retrieve("budget plan", EXEC, k=1)[0]["score"]
    above = seeded(AbstainRAG, threshold=top + 0.01)
    status = above.retrieve_with_status("budget plan", EXEC, k=3)
    assert status == {"results": [], "abstained": True, "message": "insufficient information"}
    assert above.retrieve("budget plan", EXEC, k=3) == []
    at = seeded(AbstainRAG, threshold=top).retrieve_with_status("budget plan", EXEC, k=3)
    assert at["abstained"] is False and at["results"]
    below = seeded(AbstainRAG, threshold=top - 0.01).retrieve_with_status("budget plan", EXEC, k=3)
    assert below["abstained"] is False and below["results"]


def test_abstain_decision_uses_only_visible_scores():
    rag = seeded(AbstainRAG, threshold=0.5)
    assert rag.retrieve_with_status("zephyr layoff", STAFF, k=3)["abstained"] is True
    assert rag.retrieve_with_status("zephyr layoff", EXEC, k=3)["abstained"] is False
