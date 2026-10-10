"""Live contract test: one real Messages API call through llm.ask().

Skipped unless RUN_LIVE_CONTRACT=1 and ANTHROPIC_API_KEY are both set (weekly workflow only).
Asserts structure, never wording. Not covered: the 404 -> FALLBACK_MODEL path (needs a bad-model call).
"""

import logging
import os

import llm
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_CONTRACT") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live contract test: set RUN_LIVE_CONTRACT=1 and ANTHROPIC_API_KEY",
)

CHUNKS = [{"doc_id": "fake-doc-1", "text": "Policy ZX-9 covers water damage up to 5000 dollars."}]


def test_live_ask(monkeypatch, caplog):
    monkeypatch.setattr(llm, "MAX_TOKENS", 64)
    with caplog.at_level(logging.WARNING, logger="permrag"):
        out = llm.ask("What does policy ZX-9 cover? Answer in one short sentence with a citation.", CHUNKS)
    assert "not found" not in caplog.text, "pinned model id 404'd; fell back to the alias"
    assert isinstance(out["answer"], str) and out["answer"].strip()
    usage = out["usage"]
    assert isinstance(usage["input_tokens"], int) and usage["input_tokens"] > 0
    assert isinstance(usage["output_tokens"], int) and 0 < usage["output_tokens"] <= 64
    assert "fake-doc-1" in llm._CITE.findall(out["answer"]), "parser found no [fake-doc-1] citation"
    assert out["unverified_citations"] == []
    print(f"live usage: {usage}")
