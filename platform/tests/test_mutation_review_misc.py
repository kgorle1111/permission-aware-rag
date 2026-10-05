"""Kills for reviewed surviving mutants in embeddings.embed and generation.answer."""
import logging
from unittest.mock import Mock

import pytest

from app import config, embeddings, generation


def test_embed_accepts_dimension_one_and_returns_unit_vector(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BACKEND", "hash")
    monkeypatch.setattr(config, "EMBED_DIM", 1)
    [vec] = embeddings.embed(["alpha beta gamma"])
    assert len(vec) == 1
    assert abs(vec[0]) == pytest.approx(1.0)


def test_answer_sends_exact_header_names_to_provider(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    post = Mock(side_effect=RuntimeError("stop"))
    monkeypatch.setattr(generation.httpx, "post", post)
    generation.answer("q", [{"doc_id": "d", "text": "t"}])
    # a plain dict is sent as-is; the provider contract is the lowercase names
    assert post.call_args.kwargs["headers"] == {
        "x-api-key": "k", "anthropic-version": "2023-06-01"}


def test_answer_logs_fixed_message_and_falls_back_on_provider_error(monkeypatch, caplog):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(generation.httpx, "post", Mock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.ERROR, logger="permrag"):
        out = generation.answer("q", [{"doc_id": "d1", "text": "txt"}])
    assert out == "txt [d1]"
    assert [r.getMessage() for r in caplog.records] == [
        "generation failed — falling back to extractive answer"]
