"""The rung benchmark's own contracts: honest probes, an ACL oracle that can fail, no model downloads."""
import json
from types import SimpleNamespace

import pytest

from app.ingest import chunk_document
from conftest import USERS
from scripts import rung_benchmark as rb


def test_percentile_is_nearest_rank_and_rejects_empty():
    assert rb.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.5) == 3.0
    assert rb.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.95) == 5.0
    assert rb.percentile([7.0], 0.95) == 7.0
    with pytest.raises(ValueError):
        rb.percentile([], 0.5)


def test_paraphrase_probes_share_no_content_word_with_their_target_and_targets_are_readable():
    probes = json.loads(rb.PROBES_FILE.read_text())
    by_doc = {d["doc_id"]: chunk_document(d) for d in rb._load_docs()}
    assert len(probes["paraphrase"]) >= 30 and len(probes["out_of_corpus"]) >= 10
    for p in probes["paraphrase"]:
        target = by_doc[p["doc_id"]][p["para"]]
        assert not rb._stems(p["query"]) & rb._stems(target["text"]), p["query"]
        sub, groups, _ = USERS[p["user"]]
        principals = ["*", f"user:{sub}", *[f"group:{g}" for g in groups]]
        assert rb._readable(target, principals), p
    for p in probes["lexical"]:
        assert rb._stems(p["query"]) & rb._stems(by_doc[p["doc_id"]][p["para"]]["text"]), p["query"]


def test_filtered_corpus_removes_unreadable_documents_and_sections_only():
    docs = rb._load_docs()
    guest = ["*", "user:guest@external.com"]
    kept = {d["doc_id"]: d for d in rb._filtered_corpus(docs, guest)}
    assert "hr-salaries" not in kept and "exec-comp" not in kept and "eng-runbook" not in kept
    assert "handbook" in kept and "public-it" in kept
    assert len(kept["company-strategy"]["sections"]) == 1  # finance-only section dropped
    finance = {d["doc_id"]: d for d in rb._filtered_corpus(docs, guest + ["group:finance"])}
    assert len(finance["company-strategy"]["sections"]) == 2 and "finance-ops" in finance


def test_baseline_rung_end_to_end_reports_zero_leaks_and_zero_invariance_diffs(tmp_path):
    report = rb.run_rung("baseline", str(tmp_path / "models"), {}, async_bench=True, suites=False)
    assert report["leaks"]["leaks"] == 0 and report["leaks"]["checks"] == 5 * 107
    assert report["invariance"]["diffs"] == 0 and report["invariance"]["checks"] == 3 * 107
    assert report["fail_closed_events"] == 0
    assert report["chunks"] == 37
    assert report["config"] == {"embed_backend": "hash", "sparse_backend": "none", "reranker": "none",
                                "min_score": 0.1, "rerank_min_score": -10.0}
    para, lex = report["quality"]["paraphrase"], report["quality"]["lexical"]
    assert para["n"] == 35 and para["max_shared_stems"] == 0
    assert para["hit@4"] < 0.2, "the hash baseline must not solve paraphrases; the probe set would be too easy"
    assert lex["hit@4"] >= 0.9
    modes = report["concurrency"]["modes"]
    assert set(modes) == {"sequential", "threadpool_sync", "asyncio"}
    assert all(m["wrong_or_empty_responses"] == 0 for m in modes.values())
    assert report["concurrency"]["callers"] == 20


def test_worker_failure_is_reported_not_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr(rb.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match="rung baseline worker failed"):
        rb.run_rung("baseline", str(tmp_path), {}, async_bench=False, suites=False)


def test_suite_runner_parses_counts_and_failed_ids(monkeypatch):
    out = "FAILED tests/test_side_channels.py::test_x - assert\n1 failed, 14 passed in 3.2s\n"
    monkeypatch.setattr(rb.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=out, stderr=""))
    assert rb._run_suites({}) == {"passed": 14, "failed": 1,
                                  "failed_tests": ["tests/test_side_channels.py::test_x"]}
    monkeypatch.setattr(rb.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="oops"))
    assert rb._run_suites({}) == {"passed": 0, "failed": 0, "failed_tests": []}


def _fake_report(rung, suites=None, concurrency=None, reranker="none"):
    stats = {"n": 35, "p50": 12.5, "p95": 30.0}
    q = {"n": 35, "hit@1": 0.5, "hit@4": 0.75, "mrr": 0.6, "answered": 1.0, "latency_ms": stats, "max_shared_stems": 0}
    report = {"rung": rung, "chunks": 37, "ingest_s": 1.5, "quality": {"paraphrase": q, "lexical": {**q, "n": 15, "hit@1": 1.0}},
              "out_of_corpus": {"queries": 50, "false_positive_rate": 0.2, "mean_results": 0.4},
              "leaks": {"checks": 535, "leaks": 0}, "invariance": {"checks": 321, "diffs": 0},
              "config": {"embed_backend": "fastembed", "sparse_backend": "none", "reranker": reranker,
                         "min_score": 0.55, "rerank_min_score": -10.0}}
    if suites:
        report["existing_suites"] = suites
    if concurrency:
        report["concurrency"] = concurrency
    return report


def test_table_renders_every_metric_failures_and_concurrency():
    conc = {"callers": 20, "rounds": 8, "modes": {m: {"latency_ms": {"p50": 1.0, "p95": 2.0},
                                                     "round_wall_ms_p50": 3.0, "wrong_or_empty_responses": 0}
                                                  for m in ("sequential", "threadpool_sync", "asyncio")}}
    meta = {"date": "2026-10-10", "platform": "test-os", "python": "3.12", "fastembed": "0.9.0", "qdrant": "1.19"}
    table = rb._table([_fake_report("dense", {"passed": 14, "failed": 1, "failed_tests": ["tests/t.py::x"]}),
                       _fake_report("hybrid", {"passed": 15, "failed": 0, "failed_tests": []}, conc,
                                    reranker="fastembed")], meta)
    assert ("| dense | dense 0.55 | 50% | 75% | 0.60 | 100% | 75% | 20% | 0/535 | 0/321 | 14/15 "
            "| 12.5 / 30.0 | 1.5 |") in table
    assert "| hybrid | dense 0.55, rerank -10.0 | " in table and "15/15" in table
    assert "- `dense`: `tests/t.py::x`" in table
    assert "## Concurrency (20 callers, 8 rounds, rung `hybrid`)" in table
    assert "| threadpool_sync | 1.0 | 2.0 | 3.0 | 0 |" in table
    assert "test-os, Python 3.12, fastembed 0.9.0" in table
    assert "n/a" in rb._table([_fake_report("baseline")], meta)
    assert "## How to read this" in table and "Floors are in-sample" in table


def test_main_parses_overrides_runs_each_rung_and_writes_outputs(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run_rung(rung, model_cache, overrides, async_bench, suites):
        calls.append((rung, model_cache, overrides, async_bench, suites))
        return _fake_report(rung)
    monkeypatch.setattr(rb, "run_rung", fake_run_rung)
    out = tmp_path / "results"
    assert rb.main(["--rungs", "baseline,dense", "--output-dir", str(out), "--model-cache", "/m",
                    "--set", "dense.MIN_SCORE=0.6", "--async-bench", "dense", "--skip-suites"]) == 0
    assert calls == [("baseline", "/m", {}, False, False), ("dense", "/m", {"MIN_SCORE": "0.6"}, True, False)]
    assert "| baseline | " in (out / "table.md").read_text()
    saved = json.loads((out / "report.json").read_text())
    assert [r["rung"] for r in saved["rungs"]] == ["baseline", "dense"] and saved["meta"]["date"]
    assert "| dense | " in capsys.readouterr().out


def test_version_helper_reports_missing_distributions():
    assert rb._version("nonexistent-permission-rag-package") == "not-installed"
    assert rb._version("qdrant-client") != "not-installed"
