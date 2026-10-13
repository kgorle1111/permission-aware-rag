"""F5.2: scheduled audit verification (T12): CLI, startup gate, background monitor, /healthz."""

import hashlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import audit_verify
import pytest
import underwriter_server as srv
from permission_rag import PermissionRAG

APP = pathlib.Path(__file__).parent
USER = {"id": "u", "groups": []}


def _log(tmp_path: pathlib.Path, n: int = 3) -> pathlib.Path:
    path = tmp_path / "audit.jsonl"
    rag = PermissionRAG(audit_path=path)
    rag.add_document("d", "hello world", {"*"})
    for _ in range(n):
        rag.retrieve("hello", USER)
    return path


def _run(args, env_extra, timeout=30):
    env = {k: v for k, v in os.environ.items() if k not in ("RAG_BACKEND", "DATABASE_URL")}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, *args], cwd=APP, env=env, capture_output=True, text=True, timeout=timeout
    )


def _cli(path):
    return _run(["-m", "audit_verify"], {"AUDIT_LOG_PATH": str(path)})


def _sha(line: str) -> str:
    return hashlib.sha256(line.rstrip("\n").encode()).hexdigest()


def test_cli_passes_on_an_intact_log(tmp_path):
    r = _cli(_log(tmp_path))
    assert r.returncode == 0 and "OK" in r.stdout


def test_cli_fails_on_each_tamper_with_a_precise_message(tmp_path):
    path = _log(tmp_path, n=4)
    head = PermissionRAG.audit_head_path(path)
    lines = path.read_text().splitlines(keepends=True)

    path.write_text("".join(lines[:-1]))  # tail truncation: chain intact, head ahead of the file
    r = _cli(path)
    assert r.returncode == 1 and "newest entry (3) does not match the checkpoint" in r.stderr

    path.write_text("".join(lines[:1] + [lines[1].replace('"user": "u"', '"user": "x"')] + lines[2:]))
    r = _cli(path)  # an edited line breaks the next line's prev hash
    assert r.returncode == 1 and "chain broken at entry 3" in r.stderr

    path.write_text("".join(lines))
    head.write_text("garbage")
    assert _cli(path).returncode == 1

    head.unlink()
    r = _cli(path)
    assert r.returncode == 1 and "checkpoint missing" in r.stderr

    path.write_text("")  # emptied log, checkpoint remembers an entry
    head.write_text(_sha(lines[-1]))
    r = _cli(path)
    assert r.returncode == 1 and "log truncated" in r.stderr


def test_cli_distinguishes_misconfiguration(tmp_path):
    r = _cli(tmp_path / "nope.jsonl")
    assert r.returncode == 2 and "no audit log" in r.stderr
    r = _run(["-m", "audit_verify"], {"RAG_BACKEND": "pgvector"})
    assert r.returncode == 2 and "DATABASE_URL" in r.stderr


def test_server_refuses_to_start_on_a_tampered_log(tmp_path):
    path = _log(tmp_path)
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:-1]))
    r = _run(["underwriter_server.py", "0"], {"AUDIT_LOG_PATH": str(path)}, timeout=20)
    assert r.returncode == 2
    assert "refusing to serve: audit verification failed" in r.stderr and "does not match" in r.stderr


def test_server_imports_cleanly_on_an_intact_log(tmp_path):
    r = _run(["-c", "import underwriter_server"], {"AUDIT_LOG_PATH": str(_log(tmp_path))})
    assert r.returncode == 0, r.stderr


def test_gate_flag_off_skips_the_check():
    audit_verify.require_clean_start(lambda: "boom", {"AUDIT_VERIFY_ON_START": "0"})
    with pytest.raises(SystemExit) as e:
        audit_verify.require_clean_start(lambda: "boom", {})
    assert e.value.code == 2


def test_interval_parsing():
    assert audit_verify.interval_seconds({}) == 300
    assert audit_verify.interval_seconds({"AUDIT_VERIFY_INTERVAL_S": "0"}) == 0
    for bad in ("abc", "-1", "inf", "nan"):
        with pytest.raises(ValueError):
            audit_verify.interval_seconds({"AUDIT_VERIFY_INTERVAL_S": bad})


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def served(tmp_path, monkeypatch):
    path = _log(tmp_path)
    rag = PermissionRAG(audit_path=path)
    monitor = audit_verify.AuditMonitor(rag, 0.05)
    monkeypatch.setattr(srv, "rag", rag)
    monkeypatch.setattr(srv, "AUDIT_MONITOR", monitor)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", path, monitor
    monitor.stop()
    httpd.shutdown()
    httpd.server_close()


def test_healthz_without_a_monitor_is_ok(monkeypatch):
    monkeypatch.setattr(srv, "AUDIT_MONITOR", None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert _get(f"http://127.0.0.1:{httpd.server_address[1]}", "/healthz")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_periodic_check_flips_health_and_logs_error(served, caplog):
    base, path, monitor = served
    monitor.run_once()
    code, body = _get(base, "/healthz")
    assert code == 200 and body["status"] == "ok" and body["audit_last_verified"] is not None
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:-1]))  # truncate the tail
    with caplog.at_level(logging.ERROR, logger="audit_verify"):
        monitor.run_once()
    code, body = _get(base, "/healthz")
    assert code == 503 and body["status"] == "unhealthy"
    assert "checkpoint" not in json.dumps(body)  # the reason stays in the log, not on an open endpoint
    assert any(r.levelno == logging.ERROR and "VERIFICATION FAILED" in r.getMessage() for r in caplog.records)
    path.write_text("".join(lines))  # restoring the files does not clear it: tampering is an incident
    monitor.run_once()
    assert _get(base, "/healthz")[0] == 503


def test_background_thread_runs_the_check(served):
    base, path, monitor = served
    monitor.start()
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:-1]))
    deadline = time.time() + 5
    while time.time() < deadline and _get(base, "/healthz")[0] == 200:
        time.sleep(0.05)
    assert _get(base, "/healthz")[0] == 503


def test_a_failing_check_is_unhealthy_for_that_tick_only():
    class Flaky:
        outcomes = [RuntimeError("db down"), None]

        def audit_failure(self):
            out = self.outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out

    m = audit_verify.AuditMonitor(Flaky(), 1)
    m.run_once()
    assert not m.status()["healthy"]
    m.run_once()
    assert m.status()["healthy"]


def test_verifying_a_live_log_never_sees_a_half_written_entry(tmp_path):
    """Append and head write are two steps; the monitor must not read between them."""
    rag = PermissionRAG(audit_path=tmp_path / "audit.jsonl")
    rag.add_document("d", "hello world", {"*"})
    stop, bad = threading.Event(), []

    def writer():
        while not stop.is_set():
            rag.retrieve("hello", USER)

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(200):
            if (msg := rag.audit_failure()) is not None:
                bad.append(msg)
    finally:
        stop.set()
        t.join()
    assert not bad, bad[:1]


def test_main_in_process(tmp_path, capsys):
    path = _log(tmp_path)
    env = {"AUDIT_LOG_PATH": str(path)}
    assert audit_verify.main([], env) == 0 and "OK" in capsys.readouterr().out
    path.write_text(path.read_text()[:-5])  # cut mid-entry
    assert audit_verify.main([], env) == 1 and "truncated mid-entry" in capsys.readouterr().err
    assert audit_verify.main([], {"AUDIT_LOG_PATH": str(tmp_path / "none")}) == 2
    assert audit_verify.main([], {"RAG_BACKEND": "pgvector"}) == 2
