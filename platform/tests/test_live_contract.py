"""Live contract test: one real Messages API call through generation.answer().

Skipped unless RUN_LIVE_CONTRACT=1 and ANTHROPIC_API_KEY are both set (weekly workflow only).
conftest blanks the key for offline tests and stashes the real one in _LIVE_CONTRACT_KEY.
Asserts structure, never wording. Not covered: the 404 -> fallback path (needs a bad-model call).
"""
import logging
import os
import re

import pytest

KEY = os.environ.get("_LIVE_CONTRACT_KEY", "")

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_CONTRACT") != "1" or not KEY,
    reason="live contract test: set RUN_LIVE_CONTRACT=1 and ANTHROPIC_API_KEY",
)

DOC = {"doc_id": "fake-doc-1", "text": "Policy ZX-9 covers water damage up to 5000 dollars."}


def test_live_answer(monkeypatch, caplog):
    from app import config, generation

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", KEY)
    monkeypatch.setattr(generation, "MAX_TOKENS", 64)
    with caplog.at_level(logging.WARNING, logger="permrag"):
        out = generation.answer(
            "What does policy ZX-9 cover? Answer in one short sentence with a citation.", [DOC])
    assert not out.failed, "provider call failed; got the extractive fallback"
    assert "not found" not in caplog.text, "pinned model id 404'd; fell back to the alias"
    assert out.strip() and out != f"{DOC['text']} [fake-doc-1]"
    assert isinstance(out.usage["input_tokens"], int) and out.usage["input_tokens"] > 0
    assert isinstance(out.usage["output_tokens"], int) and 0 < out.usage["output_tokens"] <= 64
    assert "fake-doc-1" in re.findall(r"\[([^\[\]\r\n]+)\]", out), "no [fake-doc-1] citation"
    print(f"live usage: {out.usage}")
