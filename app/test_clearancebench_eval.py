"""Tests for evals/clearancebench/run.py: the ACL mapping, the oracle and the planted-bug controls.

TEST SCAFFOLDING, NOT EVIDENCE. The rows below are a tiny hand-written grid in the ClearanceBench
schema. They exist so the mapping can fail in CI without the dataset; no reported number comes from them.
"""

import importlib.util
import itertools
import pathlib
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "clearancebench_run",
    pathlib.Path(__file__).resolve().parent.parent / "evals" / "clearancebench" / "run.py",
)
assert _spec is not None and _spec.loader is not None
cb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cb)

WINDOWS = [(None, None), (0.0, 400.0), (300.0, None), (600.0, 1100.0)]


def grid() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    chunks = [
        {
            "chunk_id": f"D{i}#0",
            "doc_id": f"D{i}",
            "text": f"report alpha bravo [ref CLR-D{i}-0] item {i}",
            "canary": f"CLR-D{i}-0",
            "allow_tokens": list(allow),
            "min_level": lvl,
            "not_before": nb,
            "not_after": na,
        }
        for i, (allow, lvl, (nb, na)) in enumerate(
            itertools.product([("dept:a",), ("dept:a", "proj:x"), ("proj:y",)], [0, 2], WINDOWS)
        )
    ]
    principals = [
        {"pid": f"P{j}", "level": lvl, "tokens_at_t0": list(toks)}
        for j, (toks, lvl) in enumerate(itertools.product([("dept:a",), ("proj:x",), ()], [0, 1, 3]))
    ]
    grants = cb.grants_by_pid([{"pid": "P2", "token": "proj:y", "action": "grant", "at": 100.0}])
    return chunks, principals, grants


def test_engine_enforces_exactly_the_benchmark_rules_at_every_evaluation_time() -> None:
    chunks, principals, grants = grid()
    rag = cb.build(chunks)
    engine_chunks = {c["doc_id"]: c for c in rag.chunks}
    assert len(engine_chunks) == len(chunks)  # one benchmark chunk = one engine chunk
    checked = 0
    for t in cb.TIMES:
        for p in principals:
            toks = cb.tokens_at(p, grants, t)
            for c in chunks:
                user = cb.subject(p, toks, t)
                assert rag.can_read_chunk(user, engine_chunks[c["chunk_id"]]) == cb.readable(c, p, toks, t)
                checked += 1
    assert checked == len(cb.TIMES) * len(principals) * len(chunks)


def test_oracle_rules() -> None:
    c = {"allow_tokens": ["dept:a", "proj:x"], "min_level": 2, "not_before": 100.0, "not_after": 200.0}
    p = {"pid": "P", "level": 2, "tokens_at_t0": []}
    assert cb.readable(c, p, frozenset({"proj:x"}), 100.0)  # OR over tokens, window start inclusive
    assert not cb.readable(c, p, frozenset({"proj:x"}), 200.0)  # window end exclusive
    assert not cb.readable(c, p, frozenset({"proj:z"}), 150.0)
    assert not cb.readable(c, {**p, "level": 1}, frozenset({"proj:x"}), 150.0)  # no read-up


def test_grants_apply_from_their_time_and_only_grants_are_replayed() -> None:
    p = {"pid": "P", "level": 0, "tokens_at_t0": ["org:all"]}
    g = cb.grants_by_pid([{"pid": "P", "token": "proj:x", "action": "grant", "at": 50.0}])
    assert cb.tokens_at(p, g, 49.0) == {"org:all"}
    assert cb.tokens_at(p, g, 50.0) == {"org:all", "proj:x"}
    with pytest.raises(ValueError, match="only replays grants"):
        cb.grants_by_pid([{"pid": "P", "token": "proj:x", "action": "revoke", "at": 1.0}])


@pytest.mark.parametrize("bug", ["ReadUp", "IgnoreEmbargo"])
def test_each_planted_bug_over_grants_on_its_own_axis(bug: str) -> None:
    chunks, principals, grants = grid()
    mutant = cb.build(chunks, getattr(cb, bug))
    by_id = {c["doc_id"]: c for c in mutant.chunks}
    extra = 0
    for t, p, c in itertools.product(cb.TIMES, principals, chunks):
        toks = cb.tokens_at(p, grants, t)
        if mutant.can_read_chunk(cb.subject(p, toks, t), by_id[c["chunk_id"]]) and not cb.readable(
            c, p, toks, t
        ):
            extra += 1
            assert bool(toks.intersection(c["allow_tokens"]))  # neither bug touches the token check
            if bug == "ReadUp":
                assert cb.window_open(c, t) and p["level"] < c["min_level"]
            else:
                assert p["level"] >= c["min_level"] and not cb.window_open(c, t)
    assert extra > 0


def test_detector_counts_unreadable_results_by_id_and_by_canary() -> None:
    chunks, principals, grants = grid()
    det = cb.Detector(chunks)
    p, t = principals[0], 1000.0
    toks = cb.tokens_at(p, grants, t)
    ok = next(c for c in chunks if cb.readable(c, p, toks, t))
    bad = next(c for c in chunks if not cb.readable(c, p, toks, t))
    assert det.leaks([{"doc_id": ok["chunk_id"], "text": ok["text"]}], p, toks, t) == (0, 0)
    assert det.leaks([{"doc_id": bad["chunk_id"], "text": bad["text"]}], p, toks, t) == (1, 1)
    smuggled = {
        "doc_id": ok["chunk_id"],
        "text": ok["text"] + " " + bad["canary"],
    }  # copied text from a hidden chunk
    assert det.leaks([smuggled], p, toks, t) == (0, 1)
