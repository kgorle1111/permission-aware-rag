"""Durable contracts for deterministic local embeddings and corpus replacement."""

import json
import math
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, embeddings, ingest
from app.store import ChunkACL, ChunkPolicy, SourceCheckpoint


def test_hash_embedding_has_stable_signed_ngram_buckets_and_unit_norm():
    vector = embeddings._hash_embed("alpha beta gamma", 64)
    expected_nonzero = {
        5: 1,
        6: -1,
        9: -1,
        10: 1,
        11: -1,
        17: -1,
        26: 1,
        31: 1,
        34: -1,
        39: -1,
        40: 1,
        46: -1,
        52: -1,
        55: 1,
        57: 1,
    }
    assert len(vector) == 64
    assert {index: math.copysign(1, value) for index, value in enumerate(vector) if value} == expected_nonzero
    assert sum(value * value for value in vector) == pytest.approx(1.0)
    assert embeddings._hash_embed("alpha beta gamma", 64) == vector


def test_hash_embedding_stopword_only_text_is_a_finite_zero_vector():
    vector = embeddings._hash_embed("the and or", 7)
    assert vector == [0.0] * 7
    assert all(math.isfinite(value) for value in vector)


def test_sentence_transformer_backend_loads_once_and_normalizes(monkeypatch):
    events = []

    class FakeSentenceTransformer:
        def __init__(self, model_name):
            events.append(("init", model_name))

        def encode(self, texts, *, normalize_embeddings):
            events.append(("encode", texts, normalize_embeddings))
            return SimpleNamespace(tolist=lambda: [[0.6, 0.8] for _ in texts])

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    monkeypatch.setattr(embeddings, "_st_model", None)
    monkeypatch.setattr(config, "EMBED_BACKEND", "st")

    first = embeddings.embed(["private document text"])
    second = embeddings.embed(["another query"])
    assert first == [[0.6, 0.8]]
    assert second == [[0.6, 0.8]]
    assert events == [
        ("init", "BAAI/bge-small-en-v1.5"),
        ("encode", ["private document text"], True),
        ("encode", ["another query"], True),
    ]


@pytest.mark.parametrize("backend, dimension", [("hosted", 4), ("hash", 0), ("st", -1)])
def test_embedding_rejects_unknown_backend_and_nonpositive_dimensions(
    monkeypatch, backend, dimension
):
    monkeypatch.setattr(config, "EMBED_BACKEND", backend)
    monkeypatch.setattr(config, "EMBED_DIM", dimension)
    with pytest.raises(ValueError, match="invalid embedding backend or dimension"):
        embeddings.embed(["text"])


def test_strictest_handles_both_wildcards_and_intersects_grants():
    assert ingest.strictest(["*"], ["*"]) == ["*"]
    assert ingest.strictest(["group:eng", "user:a@example.test"],
                            ["user:a@example.test", "group:hr"]) == [
        "user:a@example.test"
    ]


def test_ingest_requires_a_list_and_nonempty_unique_document_ids(tmp_path: Path):
    wrong_shape = tmp_path / "wrong-shape.json"
    wrong_shape.write_text(json.dumps({"doc_id": "one"}), encoding="utf-8")
    with pytest.raises(ValueError, match="^corpus must be a list of documents$"):
        ingest.ingest_corpus(wrong_shape)

    duplicate_ids = tmp_path / "duplicate.json"
    duplicate_ids.write_text(
        json.dumps(
            [
                {"doc_id": "same", "acl": ["*"], "sections": []},
                {"doc_id": "same", "acl": ["*"], "sections": []},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="^document ids must be nonempty and unique$"):
        ingest.ingest_corpus(duplicate_ids)


def test_incremental_ingestion_is_rejected_before_any_store_changes(tmp_path: Path, monkeypatch):
    corpus = tmp_path / "one.json"
    corpus.write_text(
        json.dumps([{"doc_id": "one", "acl": ["*"], "sections": []}]),
        encoding="utf-8",
    )
    side_effects = []
    monkeypatch.setattr(ingest, "reset_collection", lambda: side_effects.append("reset"))
    monkeypatch.setattr(ingest, "upsert_chunks", lambda *_: side_effects.append("upsert"))
    with pytest.raises(
        ValueError,
        match="^incremental ingestion is unsupported; use a full replacement$",
    ):
        ingest.ingest_corpus(corpus, reset=False)
    assert side_effects == []


def test_full_ingest_commits_barrier_revision_acl_rows_and_clears_retrieval_cache(
    tmp_path: Path, monkeypatch
):
    corpus = tmp_path / "synthetic.json"
    corpus.write_text(
        json.dumps(
            [
                {
                    "doc_id": "handbook",
                    "acl": ["*"],
                    "sections": [
                        {"text": "General procedures."},
                        {"text": "Restricted procedures.", "acl": ["group:hr"]},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    state = SimpleNamespace(revision=10, pending=False, rebuild_required=False)
    added = []
    deleted = []
    query_calls = []
    snapshots = []
    writes = []

    class FakeSession:
        def query(self, model):
            query_calls.append(model)
            return SimpleNamespace(delete=lambda: deleted.append(model))

        def add(self, row):
            added.append(row)

    session = FakeSession()
    transaction_count = 0

    @contextmanager
    def fake_transaction():
        nonlocal transaction_count
        transaction_count += 1
        yield session, state
        snapshots.append((state.revision, state.pending, state.rebuild_required))

    monkeypatch.setattr(ingest, "permission_transaction", fake_transaction)
    monkeypatch.setattr(
        ingest,
        "embed",
        lambda texts: [[float(index), 1.0] for index, _ in enumerate(texts)],
    )
    monkeypatch.setattr(ingest, "reset_collection", lambda: writes.append("reset"))
    monkeypatch.setattr(ingest, "upsert_chunks", lambda chunks: writes.append(list(chunks)))
    monkeypatch.setattr("app.retrieval.clear_cache", lambda: writes.append("clear-cache"))

    assert ingest.ingest_corpus(corpus) == 2
    assert transaction_count == 2
    assert snapshots == [(11, True, True), (12, False, False)]
    assert writes[0] == "reset"
    upserted = writes[1]
    assert [chunk["id"] for chunk in upserted] == [0, 1]
    assert [chunk["acl"] for chunk in upserted] == [["*"], ["group:hr"]]
    assert all(len(chunk["vector"]) == 2 for chunk in upserted)
    assert deleted == [ChunkACL, ChunkPolicy, SourceCheckpoint]
    assert [row.acl for row in added if isinstance(row, ChunkACL)] == [
        ["*"], ["group:hr"]
    ]
    assert len([row for row in added if isinstance(row, ChunkPolicy)]) == 2
    assert writes[-1] == "clear-cache"


def test_superseded_full_ingest_preserves_pending_rebuild_barrier(tmp_path: Path, monkeypatch):
    corpus = tmp_path / "one.json"
    corpus.write_text(
        json.dumps([{"doc_id": "one", "acl": ["*"], "sections": []}]),
        encoding="utf-8",
    )
    state = SimpleNamespace(revision=3, pending=False, rebuild_required=False)
    transactions = 0
    writes = []
    session = SimpleNamespace()

    @contextmanager
    def superseding_transaction():
        nonlocal transactions
        transactions += 1
        yield session, state
        if transactions == 1:
            state.revision += 1

    monkeypatch.setattr(ingest, "permission_transaction", superseding_transaction)
    monkeypatch.setattr(ingest, "embed", lambda _texts: [])
    monkeypatch.setattr(ingest, "reset_collection", lambda: writes.append("reset"))
    with pytest.raises(RuntimeError, match="^ingestion superseded; retry full reingestion$"):
        ingest.ingest_corpus(corpus)
    assert state.pending is True
    assert state.rebuild_required is True
    assert writes == []
