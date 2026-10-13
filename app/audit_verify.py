"""Scheduled audit-chain verification (T12): CLI, startup gate and background monitor.

Nothing used to run the chain check unless an operator did. This module is the one place that
decides which backend is configured (same env vars as underwriter_server.py), and runs the
backends' own verification (`audit_failure()`), so the CLI, the startup gate and the monitor
cannot disagree with `verify_audit_chain`.

    python -m audit_verify            # exit 0 verified, 1 tampered/truncated/unanchored, 2 misconfigured

Messages name entry/row numbers and paths, never audit contents. The periodic monitor reports
through `status()`, which the server's /healthz serves.
"""

import argparse
import logging
import os
import pathlib
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

log = logging.getLogger("audit_verify")
HERE = pathlib.Path(__file__).parent


class Verifiable(Protocol):
    def audit_failure(self) -> str | None: ...


def jsonl_path(env: Mapping[str, str] = os.environ) -> pathlib.Path:
    return pathlib.Path(env.get("AUDIT_LOG_PATH") or HERE / "audit_log.jsonl")


def pg_checkpoint_path(env: Mapping[str, str] = os.environ) -> str:
    return env.get("AUDIT_CHECKPOINT_PATH") or str(HERE / "pgvector_audit.head")


def on_start_enabled(env: Mapping[str, str] = os.environ) -> bool:
    return env.get("AUDIT_VERIFY_ON_START", "1") != "0"


def interval_seconds(env: Mapping[str, str] = os.environ) -> float:
    """AUDIT_VERIFY_INTERVAL_S: seconds between background checks; 0 disables. Bad values raise."""
    raw = env.get("AUDIT_VERIFY_INTERVAL_S", "300")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"AUDIT_VERIFY_INTERVAL_S must be a number of seconds, got {raw!r}") from None
    if not 0 <= value < float("inf"):
        raise ValueError(f"AUDIT_VERIFY_INTERVAL_S must be >= 0 (0 disables), got {raw!r}")
    return value


def require_clean_start(failure: Callable[[], str | None], env: Mapping[str, str] = os.environ) -> None:
    """Fail closed: refuse to serve on a chain that does not verify. AUDIT_VERIFY_ON_START=0 skips."""
    if not on_start_enabled(env):
        return
    message = failure()
    if message:
        log.critical("refusing to serve: audit verification failed: %s", message)
        raise SystemExit(2)


def jsonl_failure(path: pathlib.Path) -> str | None:
    """Startup/CLI check of a JSONL log; a never-written log (no file, no checkpoint) is fine."""
    from permission_rag import PermissionRAG

    if not path.exists() and not PermissionRAG.audit_head_path(path).exists():
        return None
    return PermissionRAG.locked_audit_failure(path)


class AuditMonitor:
    """Re-verifies the live backend on an interval. A definite integrity failure latches until the
    process restarts (tampering does not heal, and a flapping flag hides an incident); an error
    while checking (database briefly down) is unhealthy for that tick only."""

    def __init__(self, rag: Verifiable, interval: float) -> None:
        self.rag, self.interval = rag, interval
        self._lock = threading.Lock()
        self._latched: str | None = None
        self._transient = False
        self._last_ok: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        try:
            failure = self.rag.audit_failure()
        except Exception as e:  # any error means "could not verify", never "healthy"
            log.error("audit verification could not run: %s", type(e).__name__)
            with self._lock:
                self._transient = True
            return
        with self._lock:
            self._transient = False
            if failure:
                if self._latched is None:
                    log.error("AUDIT CHAIN VERIFICATION FAILED: %s", failure)
                self._latched = self._latched or failure
            else:
                self._last_ok = time.time()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "healthy": self._latched is None and not self._transient,
                "last_verified": self._last_ok,
            }

    def start(self) -> None:
        if self.interval <= 0 or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="audit-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.run_once()


class ConfigError(Exception):
    pass


def _cli_target(env: Mapping[str, str]) -> Callable[[], str | None]:
    if env.get("RAG_BACKEND") != "pgvector":
        path = jsonl_path(env)
        if not path.exists() and not (path.parent / (path.name + ".head")).exists():
            raise ConfigError(f"no audit log at {path} (set AUDIT_LOG_PATH, or RAG_BACKEND=pgvector)")
        return lambda: jsonl_failure(path)
    from pgvector_rag import PgVectorRAG

    if not env.get("DATABASE_URL"):
        raise ConfigError("RAG_BACKEND=pgvector needs DATABASE_URL (the app role)")
    # the key signs principals; verification never signs, so a missing RAG_PRINCIPAL_KEY is fine
    key = bytes.fromhex(env["RAG_PRINCIPAL_KEY"]) if env.get("RAG_PRINCIPAL_KEY") else secrets.token_bytes(32)
    rag = PgVectorRAG(env["DATABASE_URL"], principal_key=key, audit_checkpoint_path=pg_checkpoint_path(env))
    return rag.audit_failure


def main(argv: list[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    argparse.ArgumentParser(
        prog="audit_verify",
        description="Verify the audit hash chain and its checkpoint for the configured backend "
        "(env: RAG_BACKEND, DATABASE_URL, RAG_PRINCIPAL_KEY, AUDIT_CHECKPOINT_PATH, AUDIT_LOG_PATH).",
    ).parse_args(argv)
    try:
        failure = _cli_target(env)()
    except ConfigError as e:
        print(f"audit_verify: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"audit_verify: could not verify: {type(e).__name__}", file=sys.stderr)
        return 2
    if failure:
        print(f"AUDIT VERIFY FAILED: {failure}", file=sys.stderr)
        return 1
    print("audit verify OK: chain and checkpoint match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
