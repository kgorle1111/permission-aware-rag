"""Answer-quality metrics (ROADMAP 7.2): faithfulness, answer relevancy, context precision/recall.

Every metric asks an injected judge `judge(prompt: str) -> str` for strict JSON
{"verdicts": [bool, ...]}, one verdict per item. The judge is a seam: tests use
`offline_judge` (deterministic token overlap). A real LLM judge MUST be calibrated
against ~20 human-labeled cases (ROADMAP 7.3, human-gated) before any number from it
is reported; uncalibrated numbers from a real judge are not evidence.

Metrics never see ACLs: callers pass only context the principal may read (permission
before ranking, and before judging). Stdlib only.
"""

import json
import re
from collections.abc import Callable

Judge = Callable[[str], str]

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the and for are was were with that this from has have had not but its their they them his her "
    "you your can will would should may all any per into than then also been being who what when where".split()
)
_FORMAT = 'Return only JSON: {"verdicts": [true|false, ...]}, one boolean per item, in order.'


class JudgeError(ValueError):
    """Malformed judge output; the message says how to fix the judge or its prompt."""


def split_claims(text: str) -> list[str]:
    """Sentence-level claims. Shortcut: one sentence = one claim; compound sentences undercount."""
    return [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]


def _ask(judge: Judge, task: str, instruction: str, items: list[str], reference: str) -> list[bool]:
    payload = json.dumps({"task": task, "items": items, "reference": reference})
    raw = judge(f"{instruction}\n{_FORMAT}\n```json\n{payload}\n```")
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise JudgeError(
            f"{task}: judge output is not valid JSON ({type(exc).__name__}); "
            'make the judge return only {"verdicts": [true, false, ...]} with no prose or code fences'
        ) from exc
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, list) or any(not isinstance(v, bool) for v in verdicts):
        raise JudgeError(f'{task}: "verdicts" must be a list of booleans; got {raw[:80]!r}')
    if len(verdicts) != len(items):
        raise JudgeError(
            f"{task}: judge returned {len(verdicts)} verdicts for {len(items)} items; "
            "it must return exactly one boolean per item, in order"
        )
    return verdicts


def _nonempty(items: list[str], what: str) -> None:
    if not items:
        raise ValueError(f"{what} is empty; score abstentions/empty retrievals separately, not as 0 or 1")


def faithfulness(judge: Judge, answer: str, contexts: list[str]) -> float:
    """Supported claims / total claims in the answer, judged against the given contexts only."""
    claims = split_claims(answer)
    _nonempty(claims, "answer")
    v = _ask(
        judge, "faithfulness", "Is each claim fully supported by the context?", claims, "\n".join(contexts)
    )
    return sum(v) / len(v)


def answer_relevancy(judge: Judge, question: str, answer: str) -> float:
    """Answer claims that address the question / total claims (claim-level, judge-based)."""
    claims = split_claims(answer)
    _nonempty(claims, "answer")
    v = _ask(judge, "answer_relevancy", "Does each claim help answer the question?", claims, question)
    return sum(v) / len(v)


def context_precision(judge: Judge, ground_truth: str, contexts: list[str]) -> float:
    """Average precision over ranked contexts: mean of precision@i at each relevant rank i; 0.0 if none."""
    _nonempty(contexts, "contexts")
    instruction = "Is each ranked context useful for producing the ground-truth answer?"
    v = _ask(judge, "context_precision", instruction, contexts, ground_truth)
    hits, total = 0, 0.0
    for i, rel in enumerate(v, 1):
        if rel:
            hits += 1
            total += hits / i
    return total / hits if hits else 0.0


def context_recall(judge: Judge, ground_truth: str, contexts: list[str]) -> float:
    """Ground-truth claims found in the retrieved contexts / total ground-truth claims."""
    claims = split_claims(ground_truth)
    _nonempty(claims, "ground_truth")
    v = _ask(
        judge,
        "context_recall",
        "Is each ground-truth claim found in the context?",
        claims,
        "\n".join(contexts),
    )
    return sum(v) / len(v)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in _STOP}


def offline_judge(prompt: str) -> str:
    """Deterministic token-overlap judge for tests only. It measures word overlap, not meaning."""
    payload = json.loads(prompt.rsplit("```json\n", 1)[1].rsplit("\n```", 1)[0])
    ref = _tokens(payload["reference"])
    out = []
    for item in payload["items"]:
        toks = _tokens(item)
        if payload["task"] == "context_precision":  # useful if it covers most of the ground truth's words
            out.append(bool(ref) and len(ref & toks) / len(ref) >= 0.5)
        elif payload["task"] == "answer_relevancy":  # any shared content word with the question
            out.append(bool(ref & toks))
        else:  # claim supported if most of its words appear in the reference
            out.append(bool(toks) and len(toks & ref) / len(toks) >= 0.5)
    return json.dumps({"verdicts": out})
