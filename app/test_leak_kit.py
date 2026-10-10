"""The independent gate must reject realistic faults and meaningless adapters."""

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mutants import MUTANTS, GlobalIdf  # noqa: E402

from evals.kit.example_adapter import ExampleAdapter  # noqa: E402
from evals.kit.harness import allowed, run_suite  # noqa: E402
from evals.kit.reference_adapter import ReferenceAdapter  # noqa: E402

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "evals/kit/controls.json").read_text())


def test_independent_oracle_has_explicit_hierarchy_scope_matrix():
    expected = [
        {
            "constitution-p000",
            "constitution-p001",
            "constitution-p002",
            "constitution-p003",
            "constitution-p007",
        },
        {"constitution-p000", "constitution-p001", "constitution-p002"},
        {"constitution-p000"},
        {"constitution-p000", "constitution-p001"},
        {"constitution-p000", "constitution-p004"},
        {"constitution-p000", "constitution-p006"},
        {"constitution-p000", "constitution-p005"},
        {"constitution-p000"},
    ]
    for principal, ids in zip(FIXTURE["principals"], expected, strict=True):
        assert {c["id"] for c in FIXTURE["chunks"] if allowed(c, principal)} == ids
    for invalid in FIXTURE["invalid_chunks"]:
        assert not any(allowed(invalid, p) for p in FIXTURE["principals"])


@pytest.mark.parametrize("adapter", [ReferenceAdapter, ExampleAdapter])
def test_correct_reference_and_independent_example_pass(adapter):
    result = run_suite(adapter, FIXTURE)
    assert result["passed"], result
    assert result["probes"] == 192
    assert result["positive_controls"] == 18
    assert result["invalid_acl_controls"] == 12
    assert result["revocation_controls"] == 1


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda cls: cls.__name__)
def test_each_of_eight_planted_faults_fails(mutant):
    result = run_suite(lambda: ReferenceAdapter(mutant), FIXTURE)
    assert not result["passed"], result
    assert result["errors"] == 0, "a crash is not evidence that a planted ACL fault was observed"
    assert result["visibility_failures"] or result["isolation_failures"] or result["recall_misses"]


def test_always_empty_adapter_fails_positive_recall_and_revocation_warmup():
    class Empty(ExampleAdapter):
        def retrieve(self, query, principal, k):
            return []

    result = run_suite(Empty, FIXTURE)
    assert not result["passed"]
    assert result["recall_misses"] == 18
    assert result["revocation_failures"] == 1


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": "unknown", "score": 1}],
        [{"id": "constitution-p000", "score": float("nan")}],
        [{"id": "constitution-p000", "score": float("inf")}],
        [{"id": "constitution-p000", "score": True}],
        [{"id": "constitution-p000", "score": 1}, {"id": "constitution-p000", "score": 1}],
    ],
)
def test_bad_observations_cannot_pass(rows):
    class Broken(ExampleAdapter):
        def retrieve(self, query, principal, k):
            return rows

    result = run_suite(Broken, FIXTURE)
    assert not result["passed"]
    assert result["errors"] or result["visibility_failures"]


def test_adapter_exceptions_are_failures_not_clean_empty_results():
    class Broken(ExampleAdapter):
        def retrieve(self, query, principal, k):
            raise RuntimeError("backend offline")

    result = run_suite(Broken, FIXTURE)
    assert not result["passed"]
    assert result["errors"] > 0


def test_global_idf_is_caught_even_without_hierarchy_or_visibility_leaks():
    fixture = deepcopy(FIXTURE)
    for chunk in fixture["chunks"]:
        chunk["acl_section"] = ["*"]
        chunk["acl_para"] = ["*"]
    result = run_suite(lambda: ReferenceAdapter(GlobalIdf), fixture)
    assert not result["passed"]
    assert result["visibility_failures"] == result["errors"] == result["recall_misses"] == 0
    assert result["isolation_failures"] == 192


def test_public_corpus_snapshot_matches_manifest_and_paragraph_boundaries():
    import hashlib

    from evals.kit.prepare_public import extract

    root = Path(__file__).resolve().parents[1] / "evals/kit"
    raw = (root / "public_constitution.json").read_bytes()
    manifest = json.loads((root / "public_constitution.manifest.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == manifest["fixture_sha256"]
    fixture = json.loads(raw)
    assert len(fixture["chunks"]) == manifest["paragraphs"] == 87
    assert fixture["chunks"][0]["text"].startswith("We the People")
    assert fixture["chunks"][-1]["text"] == "Attest William Jackson Secretary"
    # Exclude page navigation and all content beyond the signed-body boundary.
    html = (
        '<p>navigation</p><p>We the People <a href="x">public</a> text.</p>'
        "<p>Attest William Jackson Secretary</p><p>done in Convention footer</p><p>other</p>"
    )
    assert extract(html) == ["We the People public text.", "Attest William Jackson Secretary"]
