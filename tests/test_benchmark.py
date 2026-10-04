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
