"""The offline benchmark reports measured evidence and has no fixed SLA."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts.benchmark import DEFAULT_CORPUS, percentile_95, run_worker


def test_percentile_95_uses_nearest_rank():
    assert percentile_95([4.0, 1.0, 3.0, 2.0]) == 4.0
    with pytest.raises(ValueError):
        percentile_95([])


def test_benchmark_smoke_isolated_and_reports_measured_metrics():
    report = run_worker(DEFAULT_CORPUS, iterations=1)

    assert report["security"]["checks_passed"] is True
    assert report["security"]["unauthorized_canary_leaks"] == 0
    assert report["security"]["revoked_cached_answer_leaks"] == 0
    assert report["forbidden_canary_chunks"] == 5
    assert report["unauthorized_probe_checks"] == 60
    assert report["probe_variants"] == [
        "full_paragraph", "canary_only", "instruction_injection"]
    assert report["principals_checked"] == ["alice", "bob", "carol", "ceo", "guest"]
    for path in ("cold", "warm_cache"):
        metric = report["latency_ms"][path]
        assert metric["samples"] == 5
        assert isinstance(metric["p95"], (int, float))
        assert metric["p95"] >= 0
    assert report["latency_ms"]["single_sync_revoke"]["changed_docs"] == ["hr-salaries"]
    assert report["method"]["performance_targets"] is None
    assert len(report["corpus_sha256"]) == 64
    assert datetime.fromisoformat(report["timestamp_utc"]).tzinfo is not None
    assert report["runtime"]["python"]
    assert report["runtime"]["packages"]["qdrant-client"]
    assert report["method"]["settings"]["permissions_backend"] == "jsonl"
    # Assert output is JSON serializable for piping or --output consumers.
    assert json.loads(json.dumps(report))["benchmark"] == "permission-aware-rag-offline"


def test_benchmark_rejects_missing_corpus_and_invalid_iterations(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_worker(tmp_path / "missing.json", iterations=1)
    with pytest.raises(ValueError):
        run_worker(Path(DEFAULT_CORPUS), iterations=0)


def test_benchmark_oracle_rejects_unknown_unauthorized_and_answer_leaks():
    from app.identity import Principal
    from scripts.benchmark import _validate_results
    principal = Principal("guest")
    chunks = [{"doc_id": "secret", "text": "sensitive", "acl": ["group:hr"]}]
    with pytest.raises(AssertionError, match="unknown chunk"):
        _validate_results({"results": [{"doc_id": "unknown", "text": "arbitrary"}]}, principal, chunks)
    with pytest.raises(AssertionError, match="unauthorized result"):
        _validate_results({"results": [{"doc_id": "secret", "text": "sensitive"}]}, principal, chunks)
    with pytest.raises(AssertionError, match="forbidden canary"):
        _validate_results({"results": [], "answer": "CANARY-SECRET"}, principal, chunks,
                          forbidden_markers={"CANARY-SECRET"})


def test_benchmark_worker_errors_do_not_produce_a_success_report(monkeypatch):
    from scripts import benchmark
    from types import SimpleNamespace
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=1, stdout="", stderr="worker failed"))
    with pytest.raises(RuntimeError, match="worker failed"):
        benchmark.run_worker(DEFAULT_CORPUS, 1)
    assert benchmark._package_version("nonexistent-permission-rag-package") == "not-installed"


def test_benchmark_oracle_preserves_independent_acl_levels():
    from scripts.benchmark import _chunk_can_read
    chunk={"acl":[], "acl_doc":["group:hr"],"acl_section":["user:bob"],"acl_para":["*"]}
    assert _chunk_can_read(chunk,["*","user:bob","group:hr"])
    assert not _chunk_can_read(chunk,["*","user:bob"])
    assert not _chunk_can_read(chunk,["*","user:alice","group:hr"])
    del chunk["acl_para"]
    assert not _chunk_can_read(chunk,["*","user:bob","group:hr"])
