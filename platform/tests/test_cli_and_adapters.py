"""Executable command and optional-provider contracts, without external services."""
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import jwt
import numpy as np
import pytest

from app import config
from conftest import auth, reingest

ROOT = Path(__file__).resolve().parents[1]


def run_script(monkeypatch, name, *args):
    monkeypatch.setattr(sys, "argv", [name, *args])
    return runpy.run_path(str(ROOT / "scripts" / name), run_name="__main__")


def test_keygen_creates_verifiable_private_keys_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    private, public = tmp_path / "keys/private.pem", tmp_path / "keys/public.pem"
    monkeypatch.setattr(config, "JWT_PRIVATE_KEY_PATH", str(private))
    monkeypatch.setattr(config, "JWT_PUBLIC_KEY_PATH", str(public))
    mask = os.umask(0o022)
    try:
        run_script(monkeypatch, "gen_keys.py")
    finally:
        os.umask(mask)
    signed = jwt.encode({"sub": "test"}, private.read_text(), algorithm="RS256")
    assert jwt.decode(signed, public.read_text(), algorithms=["RS256"])["sub"] == "test"
    assert private.stat().st_mode & 0o777 == 0o600
    before = private.read_bytes()
    with pytest.raises(SystemExit, match="already exist"):
        run_script(monkeypatch, "gen_keys.py")
    assert private.read_bytes() == before
    assert "wrote" in capsys.readouterr().out


def test_token_cli_uses_claims_and_configured_issuer(monkeypatch, capsys):
    monkeypatch.setattr(config, "JWT_ISSUER", "test-issuer")
    run_script(monkeypatch, "mint_token.py", "bob@example.test", "--groups", "hr", "--ttl", "90")
    token = capsys.readouterr().out.strip()
    claims = jwt.decode(token, Path(config.JWT_PUBLIC_KEY_PATH).read_text(),
                        algorithms=["RS256"], audience=config.JWT_AUDIENCE, issuer="test-issuer")
    assert claims["sub"] == "bob@example.test"
    assert claims["groups"] == ["hr"]
    assert claims["exp"] - claims["iat"] == 90


def test_ingest_and_sync_commands_run_real_local_pipeline(client, monkeypatch, capsys):
    try:
        run_script(monkeypatch, "ingest.py", str(ROOT / "corpus/docs.json"))
        assert "ingested 12 chunks" in capsys.readouterr().out
        run_script(monkeypatch, "sync_run.py")
        assert "changed docs: none" in capsys.readouterr().out
        from app import sync
        observed = []
        monkeypatch.setattr(sync, "watch", lambda interval: observed.append(interval))
        run_script(monkeypatch, "sync_run.py", "--watch", "2.5")
        assert observed == [2.5]
        assert client.post("/query", headers=auth("bob"), json={"query": "salary bands"}).json()["results"]
    finally:
        reingest()


def test_demo_script_exercises_actual_api(client, monkeypatch, capsys):
    # Loopback through the ASGI transport, preserving JWT verification and ACLs.
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: client.post("/query", **kwargs))
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: client.get("/audit", **kwargs))
    run_script(monkeypatch, "demo.py")
    output = capsys.readouterr().out
    assert "alice@company.com" in output and "bob@company.com" in output
    assert "guest@external.com" in output and "90k to 250k" in output
    assert "audit (as security)" in output and "denied" in output


def test_generation_sends_only_supplied_context_and_falls_back_on_provider_error(monkeypatch):
    from app import generation
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-test-key")
    results = [{"doc_id": "public-doc", "text": "permitted material"}]
    post = Mock(return_value=httpx.Response(200, request=httpx.Request("POST", "https://example.test"),
                                           json={"content": [{"text": "grounded answer [public-doc]"}]}))
    monkeypatch.setattr(generation.httpx, "post", post)
    assert generation.answer("question", results) == "grounded answer [public-doc]"
    sent = post.call_args.kwargs
    assert sent["json"]["messages"][0]["content"] == f'Context:\n<document id="public-doc">\npermitted material\n</document>\n\n{generation.REMINDER}\n\nQuestion: question'
    assert sent["headers"]["x-api-key"] == "fake-test-key" and sent["timeout"] == 30
    post.side_effect = httpx.TimeoutException("unavailable")
    assert generation.answer("question", results) == "permitted material [public-doc]"
    assert generation.answer("question", []) == "No results found."


def test_sentence_transformer_is_cached_and_normalizes_local_vectors(monkeypatch):
    from app import embeddings
    model = Mock()
    model.encode.return_value = np.array([[1., 0.]])
    constructor = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor))
    monkeypatch.setattr(embeddings, "_st_model", None)
    monkeypatch.setattr(config, "EMBED_BACKEND", "st")
    assert embeddings.embed(["local text"]) == [[1., 0.]]
    assert embeddings.embed(["again"]) == [[1., 0.]]
    constructor.assert_called_once_with("BAAI/bge-small-en-v1.5")
    model.encode.assert_called_with(["again"], normalize_embeddings=True)
    monkeypatch.setattr(config, "EMBED_BACKEND", "unknown")
    with pytest.raises(ValueError, match="invalid embedding"):
        embeddings.embed(["test"])


def test_remote_vector_factory_and_explicit_close(monkeypatch):
    from app import vectorstore
    fake = Mock()
    factory = Mock(return_value=fake)
    monkeypatch.setattr(vectorstore, "QdrantClient", factory)
    monkeypatch.setattr(vectorstore, "_client", None)
    monkeypatch.setattr(config, "QDRANT_URL", "http://vector.example.test:6333")
    assert vectorstore.client() is fake
    assert vectorstore.client() is fake
    factory.assert_called_once_with(url=config.QDRANT_URL)
    vectorstore.close_client()
    fake.close.assert_called_once()
    assert vectorstore._client is None
    vectorstore.close_client()


def test_demo_routes_mint_real_scope_tokens_and_readiness(client, monkeypatch):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/readyz").json() == {"ready": True}
    assert "Permission-Aware RAG" in client.get("/").text
    monkeypatch.setattr(config, "DEMO_MODE", True)
    tokens = client.get("/demo/tokens").json()
    assert len(tokens) == 5
    from app.identity import verify_token
    assert verify_token(tokens["bob (hr)"]).groups == ("hr",)
    assert verify_token(tokens["guest"]).groups == ()
    from app import main
    monkeypatch.setattr(main, "SessionLocal", Mock(side_effect=RuntimeError("DB unavailable")))
    assert client.get("/readyz").status_code == 503


def test_benchmark_worker_checks_security_and_emits_json(client, monkeypatch, tmp_path, capsys):
    import json
    from scripts import benchmark
    monkeypatch.setenv("JWT_PRIVATE_KEY_PATH", str(tmp_path / "private.pem"))
    monkeypatch.setenv("JWT_PUBLIC_KEY_PATH", str(tmp_path / "public.pem"))
    try:
        assert benchmark.main(["--_worker", str(benchmark.DEFAULT_CORPUS), "1"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["unauthorized_probe_checks"] == 60
        assert report["security"] == {"checks_passed": True, "unauthorized_canary_leaks": 0,
                                      "revoked_cached_answer_leaks": 0}
        assert report["latency_ms"]["cold"]["samples"] == 5
    finally:
        reingest()


def test_benchmark_cli_report_and_worker_failure(monkeypatch, tmp_path, capsys):
    from scripts import benchmark
    output = tmp_path / "results/report.json"
    monkeypatch.setattr(benchmark, "run_worker", lambda *args: {"checked": True})
    assert benchmark.main(["--output", str(output)]) == 0
    assert '"checked": true' in output.read_text()
    assert '"checked": true' in capsys.readouterr().out


def test_drive_watch_cli_and_invalid_provider_response(monkeypatch, tmp_path, capsys):
    from scripts import drive_watch
    from unittest.mock import MagicMock
    drive = MagicMock()
    drive.changes().getStartPageToken().execute.return_value = {"startPageToken": "start"}
    drive.changes().watch().execute.return_value = {"resourceId": "resource", "expiration": "999"}
    monkeypatch.setattr(drive_watch, "build_drive_client", lambda *args: drive)
    output = tmp_path / "channel.env"
    monkeypatch.setattr(sys, "argv", ["watch", "--address", "https://example.test/webhooks/drive", "--output", str(output)])
    drive_watch.main()
    assert output.exists() and "saved privately" in capsys.readouterr().out
    with pytest.raises(ValueError, match="lifetime"):
        drive_watch.register(drive, "https://example.test", tmp_path / "badttl", 200)
    for index, resource_id in enumerate(["", "invalid\nresource"]):
        drive.changes().watch().execute.return_value = {"resourceId": resource_id}
        with pytest.raises(ValueError, match="resource ID"):
            drive_watch.register(drive, "https://example.test", tmp_path / f"bad{index}")
