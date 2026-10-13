"""T15 mitigation: claim-level citation coverage. Mitigation, not a solution: it checks that a
factual sentence carries a citation to a RETRIEVED document, not that the claim is true."""

import io
import json
from unittest import mock

import llm

IDS = {"policy-1", "claims-1"}


def test_flags_each_sentence_without_a_retrieved_citation():
    answer = (
        "Policy 1 is active [policy-1]. Premium is paid through December 2026. "
        "The claim paid 12400 dollars [watchlist]. One water claim exists [claims-1]."
    )
    assert llm.uncited_claims(answer, IDS) == [
        "Premium is paid through December 2026.",
        "The claim paid 12400 dollars [watchlist].",
    ]


def test_refusal_headings_and_trailing_citation_are_not_claims():
    answer = (
        "Findings for human review:\n"
        "- Policy 1 is active. [policy-1]\n"
        "The documents you have access to do not answer this."
    )
    assert llm.uncited_claims(answer, IDS) == []


def test_bullets_are_split_per_line_and_decimals_do_not_split():
    answer = "- Coverage is 2.1 million dollars [policy-1]\n- Renewal is blocked pending inspection"
    assert llm.uncited_claims(answer, IDS) == ["- Renewal is blocked pending inspection"]


def _resp(answer):
    body = {"content": [{"type": "text", "text": answer}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    return io.BytesIO(json.dumps(body).encode())


def test_ask_returns_uncited_claims_next_to_unverified_citations():
    chunks = [{"doc_id": "policy-1", "text": "Policy 1 is active."}]
    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "t"}), mock.patch("urllib.request.urlopen") as u:
        u.return_value.__enter__.return_value = _resp(
            "Policy 1 is active [policy-1]. It was never audited by anyone."
        )
        out = llm.ask("q", chunks)
    assert out["uncited_claims"] == ["It was never audited by anyone."]
    assert out["unverified_citations"] == []
