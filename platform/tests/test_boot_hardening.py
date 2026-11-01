"""Boot-time identity validation and the request-body cap.

A garbage key or unreachable JWKS must stop startup (not 401 every request), and an oversized body
must be refused with 413 before the app buffers it, whether or not it declares a Content-Length.
"""
import asyncio
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import config, identity, main
from app.identity import Principal
from conftest import _key, auth, mint

ROOT = Path(__file__).resolve().parent.parent
NO_IDP = ("no identity provider configured: set JWKS_URL to your IdP's JWKS endpoint, "
          "or DEMO_MODE=1 for a local demo")
CAP = 65536  # literal on purpose: a mutated MAX_BODY_BYTES must not move the test with it


@pytest.fixture()
def boot(monkeypatch, tmp_path):
    """Local-key mode with an empty cache; returns a path to write a candidate public key to."""
    monkeypatch.setattr(config, "JWKS_URL", "")
    monkeypatch.setattr(config, "DEMO_MODE", False)
    monkeypatch.setattr(identity, "_public_key_cache", None)
    monkeypatch.setattr(identity, "_jwks_client", None)
    path = tmp_path / "idp_public.pem"
    monkeypatch.setattr(config, "JWT_PUBLIC_KEY_PATH", str(path))
    return path


def _public_pem(key=_key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


# ---- fail loud at boot -------------------------------------------------------------------------

def test_garbage_public_key_file_prevents_startup(boot):
    boot.write_text("-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----\n")
    with pytest.raises(RuntimeError) as error:
        with TestClient(main.app):
            pass
    message = str(error.value)
    assert message.startswith(f"JWT_PUBLIC_KEY_PATH={boot} is not a usable RSA public key PEM (")
    assert message.endswith("): mount your IdP's RS256 public key there, or set JWKS_URL instead")
    assert identity._public_key_cache is None


@pytest.mark.parametrize("content", [
    b"",
    _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                       serialization.NoEncryption()),
    b"\xff\xfe not utf-8",
], ids=["empty", "private-key-at-public-path", "binary"])
def test_unusable_public_key_contents_are_refused(boot, content):
    boot.write_bytes(content)
    with pytest.raises(RuntimeError, match="is not a usable RSA public key PEM"):
        identity.validate_identity_config()
    assert identity._public_key_cache is None


def test_non_rsa_public_key_is_refused(boot):
    boot.write_bytes(_public_pem(ec.generate_private_key(ec.SECP256R1())))
    with pytest.raises(RuntimeError, match=r"\(ValueError: not an RSA key\)"):
        identity.validate_identity_config()


def test_valid_public_key_is_loaded_at_boot(boot):
    boot.write_bytes(_public_pem())
    identity.validate_identity_config()
    assert identity._public_key_cache == boot.read_text()
    assert identity.verify_token(mint("boot@example.test", ["eng"])) == Principal(
        "boot@example.test", ("eng",))


def test_no_identity_provider_outside_demo_mode_is_refused(boot):
    with pytest.raises(RuntimeError) as error:
        identity.validate_identity_config()
    assert str(error.value) == NO_IDP
    assert identity.NO_IDP_MESSAGE == NO_IDP


def test_demo_mode_without_a_local_keypair_points_at_gen_keys(boot, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with pytest.raises(RuntimeError) as error:
        identity.validate_identity_config()
    assert str(error.value) == (
        f"DEMO_MODE=1 but JWT_PUBLIC_KEY_PATH={boot} does not exist: "
        "run python scripts/gen_keys.py to create a local demo keypair")


def test_demo_mode_requires_the_private_key_it_mints_with(boot, monkeypatch, tmp_path):
    boot.write_bytes(_public_pem())
    private = tmp_path / "idp_private.pem"
    monkeypatch.setattr(config, "JWT_PRIVATE_KEY_PATH", str(private))
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with pytest.raises(RuntimeError) as error:
        identity.validate_identity_config()
    assert str(error.value) == (
        f"DEMO_MODE=1 needs the local private key at JWT_PRIVATE_KEY_PATH={private} to mint demo "
        "tokens: run python scripts/gen_keys.py with fresh key paths, or set DEMO_MODE=0")
    assert identity._public_key_cache is None

    private.write_bytes(b"present")
    identity.validate_identity_config()
    assert identity._public_key_cache == boot.read_text()


def test_production_mode_does_not_need_a_private_key(boot, monkeypatch, tmp_path):
    boot.write_bytes(_public_pem())
    monkeypatch.setattr(config, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "absent.pem"))
    identity.validate_identity_config()
    assert identity._public_key_cache == boot.read_text()


def test_demo_mode_with_jwks_url_is_refused_before_any_fetch(boot, monkeypatch):
    def no_client(*_args, **_kwargs):
        raise AssertionError("must refuse before contacting the IdP")

    monkeypatch.setattr(identity.jwt, "PyJWKClient", no_client)
    monkeypatch.setattr(config, "JWKS_URL", "https://idp.example.test/jwks")
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with pytest.raises(RuntimeError) as error:
        identity.validate_identity_config()
    assert str(error.value) == (
        "DEMO_MODE=1 cannot be combined with JWKS_URL: demo mode mints its own tokens. "
        "Set DEMO_MODE=0 for a real IdP, or unset JWKS_URL for a local demo")


def test_unreachable_jwks_url_prevents_startup(boot, monkeypatch):
    # a port nothing listens on: the real PyJWKClient fails fast with a connection error
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}/jwks"
    monkeypatch.setattr(config, "JWKS_URL", url)
    with pytest.raises(RuntimeError) as error:
        with TestClient(main.app):
            pass
    message = str(error.value)
    assert message.startswith(f"JWKS_URL={url} could not be loaded (PyJWKClientConnectionError: ")
    assert message.endswith(
        "): set JWKS_URL to your IdP's reachable JWKS endpoint serving an RS256 signing key")
    assert identity._jwks_client is None


def test_jwks_boot_fetch_is_bounded_and_its_client_is_reused(boot, monkeypatch):
    built = []

    class FakeClient:
        def __init__(self, url, timeout):
            built.append((url, timeout))
            self.fetches = 0

        def get_signing_keys(self):
            self.fetches += 1
            return ["key"]

    monkeypatch.setattr(identity.jwt, "PyJWKClient", FakeClient)
    monkeypatch.setattr(config, "JWKS_URL", "https://idp.example.test/jwks")
    identity.validate_identity_config()
    assert built == [("https://idp.example.test/jwks", 5)]
    assert identity._jwks_client.fetches == 1
    assert identity._public_key() is identity._jwks_client
    assert len(built) == 1, "request-time verification must reuse the boot client"


class _JwksHandler(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        pass


def test_real_jwks_endpoint_boots_and_verifies_tokens(boot, monkeypatch):
    jwk = pyjwt.algorithms.RSAAlgorithm.to_jwk(_key.public_key(), as_dict=True)
    _JwksHandler.body = json.dumps(
        {"keys": [{**jwk, "kid": "boot-key", "use": "sig", "alg": "RS256"}]}).encode()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(config, "JWKS_URL", f"http://127.0.0.1:{server.server_port}/jwks")
        identity.validate_identity_config()
        token = pyjwt.encode(
            pyjwt.decode(mint("jwks@example.test", ["hr"]), options={"verify_signature": False}),
            _key, algorithm="RS256", headers={"kid": "boot-key"})
        assert identity.verify_token(token) == Principal("jwks@example.test", ("hr",))
    finally:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _uvicorn(port: int, **env):
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=ROOT, env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@pytest.mark.parametrize("key_contents,demo,expected", [
    (b"garbage", "0", "is not a usable RSA public key PEM"),
    (None, "0", NO_IDP),
], ids=["garbage-key", "no-idp"])
def test_uvicorn_refuses_to_start_with_a_prescriptive_error(tmp_path, key_contents, demo, expected):
    public = tmp_path / "idp_public.pem"
    if key_contents is not None:
        public.write_bytes(key_contents)
    proc = _uvicorn(_free_port(), JWT_PUBLIC_KEY_PATH=str(public), DEMO_MODE=demo, JWKS_URL="")
    try:
        output, _ = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("uvicorn started serving instead of refusing to boot")
    assert proc.returncode != 0
    assert expected in output.decode()


# ---- request body cap --------------------------------------------------------------------------

def test_one_megabyte_body_gets_413_before_the_app(client, monkeypatch):
    reached = []
    monkeypatch.setattr(main, "retrieve_checked", lambda *a, **kw: reached.append(1))
    body = json.dumps({"query": "x" * (1024 * 1024)})
    response = client.post("/query", content=body, headers={
        **auth("bob"), "Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}
    assert response.headers["X-Request-ID"]
    assert reached == []


def test_normal_query_still_works(client):
    response = client.post("/query", json={"query": "what are the salary bands?"},
                           headers=auth("bob"))
    assert response.status_code == 200
    assert any(r["doc_id"] == "hr-salaries" for r in response.json()["results"])


def _chunks(total: int, size: int = 8192):
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b"x" * n


def test_chunked_oversized_body_gets_413(client):
    response = client.post("/query", content=_chunks(1024 * 1024), headers={
        **auth("bob"), "Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


def test_chunked_small_body_is_replayed_to_the_app(client):
    payload = json.dumps({"query": "what are the salary bands?"}).encode()

    def two_chunks():
        yield payload[:10]
        yield payload[10:]

    response = client.post("/query", content=two_chunks(), headers={
        **auth("bob"), "Content-Type": "application/json"})
    assert response.status_code == 200
    assert any(r["doc_id"] == "hr-salaries" for r in response.json()["results"])


@pytest.mark.parametrize("size,status", [(CAP, 422), (CAP + 1, 413)])
def test_cap_boundary_for_declared_and_streamed_bodies(client, size, status):
    body = b'{"query": "' + b"x" * (size - 13) + b'"}'
    assert len(body) == size
    declared = client.post("/query", content=body, headers=auth("bob"))
    streamed = client.post("/query", content=_chunks(size), headers=auth("bob"))
    assert declared.status_code == status
    # the streamed body is not JSON, so under the cap the app rejects it as 422
    assert streamed.status_code == status


def _run(scope, messages):
    sent, calls = [], []
    queue = list(messages)

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        calls.append([await receive(), await receive()])

    asyncio.run(main.BodySizeLimit(app)(scope, receive, send))
    return sent, calls


def _http(headers=()):
    return {"type": "http", "method": "POST", "path": "/query", "headers": list(headers)}


@pytest.mark.parametrize("declared", [b"12abc", str(CAP + 1).encode()])
def test_bad_or_oversized_content_length_is_rejected_without_reading(declared):
    # an empty receive queue: any read of the body would raise
    sent, calls = _run(_http([(b"content-length", declared)]), [])
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b'{"detail":"request body too large"}'
    assert calls == []


def test_declared_length_at_the_cap_is_read_and_passed_on():
    body = b"x" * CAP
    sent, calls = _run(_http([(b"content-length", str(CAP).encode())]), [
        {"type": "http.request", "body": body}, {"type": "http.disconnect"}])
    assert sent == []
    assert calls[0][0]["body"] == body


def test_disconnect_mid_body_skips_the_app_and_still_answers():
    sent, calls = _run(_http(), [
        {"type": "http.request", "body": b"x", "more_body": True}, {"type": "http.disconnect"}])
    assert calls == []
    assert sent[0]["status"] == 400
    assert sent[1]["body"] == b'{"detail":"client disconnected"}'


def test_disconnect_mid_body_is_not_a_server_error(client):
    # through the full stack: BaseHTTPMiddleware raises "No response returned" if nothing answers
    messages = [{"type": "http.request", "body": b"{", "more_body": True},
                {"type": "http.disconnect"}]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/query", "raw_path": b"/query", "query_string": b"",
             "root_path": "", "headers": [(b"content-type", b"application/json")],
             "client": ("127.0.0.1", 1), "server": ("testserver", 80), "state": {}}
    asyncio.run(main.app(scope, receive, send))
    assert sent[0]["status"] == 400


def test_replay_defaults_and_delegates_after_the_buffered_body():
    disconnect = {"type": "http.disconnect"}
    sent, calls = _run(_http(), [
        {"type": "http.request", "more_body": True},  # no "body" key
        {"type": "http.request", "body": b"ab"},  # no "more_body" key: last chunk
        disconnect])
    assert sent == []
    assert calls == [[{"type": "http.request", "body": b"ab", "more_body": False}, disconnect]]


def test_non_http_scopes_pass_through_untouched():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(main.BodySizeLimit(app)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_uvicorn_rejects_chunked_oversized_body_and_replays_small_ones(tmp_path):
    port = _free_port()
    proc = _uvicorn(port)

    def request(method, path, chunks=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=chunks, encode_chunked=chunks is not None,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if request("GET", "/healthz")[0] == 200:
                    break
            except OSError:
                assert proc.poll() is None, "uvicorn exited during startup"
                assert time.monotonic() < deadline
                time.sleep(0.2)

        status, body = request("POST", "/query", _chunks(1024 * 1024))
        assert status == 413 and json.loads(body) == {"detail": "request body too large"}
        # small chunked body reaches the app (no token, so 401 rather than 413)
        status, _ = request("POST", "/query", iter([b'{"query":', b' "hello"}']))
        assert status == 401
    finally:
        proc.terminate()
        proc.communicate(timeout=30)
