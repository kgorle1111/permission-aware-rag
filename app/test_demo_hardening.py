"""Hardening of the deployed demo server: ingest, connection bounds, shutdown, errors, JWT logging."""

import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import audit_verify
import pytest
import underwriter_server as srv
from permission_rag import PermissionRAG

APP = pathlib.Path(__file__).parent

srv.rag.audit_path = None  # tests must not write the real audit log


@pytest.fixture
def base() -> Iterator[str]:
    server = srv.BoundedServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, dict[str, object]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def _post(url: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


# 1. ingest


def test_ingest_corpus_fails_loudly_on_a_bad_acl() -> None:
    store = PermissionRAG()
    with pytest.raises(ValueError, match="bad acl entry"):
        srv.ingest_corpus(store, [("ok", "fine text", {"*"}), ("bad", "lost text", {"everyone"})])


def test_ingest_corpus_skips_only_documents_already_present() -> None:
    store = PermissionRAG()
    srv.ingest_corpus(store, srv.CORPUS)
    n = len(store.chunks)
    srv.ingest_corpus(store, [*srv.CORPUS, ("new-doc", "new text", {"*"})])  # restart over a kept store
    assert len(store.chunks) == n + 1
    assert srv.existing_doc_ids(store) == {d for d, _, _ in srv.CORPUS} | {"new-doc"}


# 2. connection bounds


def test_handler_socket_timeout_is_set() -> None:
    assert srv.Handler.timeout == 10


def test_idle_connection_is_closed_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv.Handler, "timeout", 0.5)
    server = srv.BoundedServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as idle:
            t0 = time.monotonic()
            assert idle.recv(1) == b""  # server closed the connection; no request was ever sent
            assert time.monotonic() - t0 < 3
    finally:
        server.shutdown()
        server.server_close()


def test_inflight_cap_returns_503_then_recovers() -> None:
    server = srv.BoundedServer(("127.0.0.1", 0), srv.Handler, max_inflight=1)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/healthz"
    try:
        # holds the only slot
        hog = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
        time.sleep(0.3)
        assert _get(url) == (503, {"error": "server busy"})
        hog.close()
        deadline = time.monotonic() + 5
        while (status := _get(url)[0]) == 503 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


# 3 + 5. graceful shutdown and the startup line


def test_sigterm_lets_the_in_flight_request_finish(tmp_path: pathlib.Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    env = {k: v for k, v in os.environ.items() if k not in ("RAG_BACKEND", "DATABASE_URL")}
    env.update({"AUDIT_LOG_PATH": str(log_path), "HOST": "127.0.0.1"})
    with subprocess.Popen(
        [sys.executable, "underwriter_server.py", "0"],
        cwd=APP,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            assert proc.stdout is not None and proc.stderr is not None
            line = proc.stdout.readline()
            m = re.fullmatch(r"listening on http://127\.0\.0\.1:(\d+)\n", line)
            assert m, line
            body = json.dumps({"user": "junior", "q": "policy 10023"}).encode()
            with socket.create_connection(("127.0.0.1", int(m.group(1))), timeout=10) as c:
                c.sendall(
                    b"POST /query HTTP/1.0\r\nContent-Type: application/json\r\n"
                    b"Content-Length: %d\r\n\r\n" % len(body)
                )
                time.sleep(0.5)  # the handler is now blocked reading the body
                proc.send_signal(signal.SIGTERM)
                time.sleep(0.5)
                c.sendall(body)
                response = b""
                while chunk := c.recv(65536):
                    response += chunk
            assert response.startswith(b"HTTP/1.0 200"), response[:200]
            assert proc.wait(timeout=15) == 0, proc.stderr.read()
        finally:
            if proc.poll() is None:
                proc.kill()
    assert audit_verify.jsonl_failure(log_path) is None
    entries = [json.loads(x) for x in log_path.read_text().splitlines()]
    assert [e["user"] for e in entries] == ["junior"]


# 4. unknown role


def test_unknown_role_is_named_and_missing_fields_keep_their_message(base: str) -> None:
    status, body = _get(base + "/query?user=nobody&q=policy")
    assert (status, body["error"]) == (400, "unknown role")
    status, body = _post(base + "/ask", {"user": "nobody", "q": "policy"})
    assert (status, body["error"]) == (400, "unknown role")
    for path in ("/query?q=policy", "/query?user=junior", "/query?user=nobody"):
        status, body = _get(base + path)
        assert (status, body["error"]) == (400, "need user and q"), path


# 6. JWT rejection reasons


def _jwt(secret: str, claims: dict[str, object]) -> str:
    def enc(o: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    h, p = enc({"alg": "HS256", "typ": "JWT"}), enc(claims)
    sig = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        ("", "missing"),
        ("Bearer not-a-jwt", "malformed"),
        (
            f"Bearer {_jwt('other-secret', {'sub': 'u', 'groups': [], 'exp': time.time() + 365 * 86400})}",
            "bad signature",
        ),
        (f"Bearer {_jwt('s3cret', {'sub': 'u', 'groups': [], 'exp': time.time() - 5})}", "expired"),
        (
            f"Bearer {_jwt('s3cret', {'sub': 'a,b', 'groups': [], 'exp': time.time() + 365 * 86400})}",
            "malformed",
        ),
    ],
    ids=["missing", "malformed", "bad-signature", "expired", "bad-claims"],
)
def test_jwt_rejection_logs_the_reason_never_the_token(
    header: str, reason: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(srv, "JWT_SECRET", "s3cret")
    with caplog.at_level("WARNING", logger="underwriter"):
        assert srv.user_from_jwt(header) is None
    assert [r.getMessage() for r in caplog.records] == [f"jwt rejected: {reason}"]
    token = header.removeprefix("Bearer ")
    assert not token or token not in caplog.text


def test_valid_jwt_logs_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(srv, "JWT_SECRET", "s3cret")
    tok = _jwt("s3cret", {"sub": "u", "groups": ["g"], "exp": time.time() + 60})
    with caplog.at_level("WARNING", logger="underwriter"):
        assert srv.user_from_jwt(f"Bearer {tok}") == {"id": "u", "groups": ["g"]}
    assert not caplog.records
