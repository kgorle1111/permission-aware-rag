"""T12 / ROADMAP B22: several app processes appending to one JSONL audit log keep one valid chain."""

import json
import multiprocessing
import pathlib
import sys
import time

import pytest
from permission_rag import PermissionRAG

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="cross-process flock is POSIX only; Windows is thread-safe only"
)
USER = {"id": "u", "groups": []}


def _worker(path: str, uid: str, n: int) -> None:
    rag = PermissionRAG(audit_path=path)
    for _ in range(n):
        rag.retrieve("anything", {"id": uid, "groups": []})


def test_processes_share_one_chain_and_checkpoint(tmp_path):
    path = tmp_path / "audit.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker, args=(str(path), f"p{i}", 20)) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert [p.exitcode for p in procs] == [0] * 4
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(entries) == 80
    assert {e["user"] for e in entries} == {"p0", "p1", "p2", "p3"}
    assert PermissionRAG.audit_file_failure(path) is None


def test_instance_does_not_trust_its_in_memory_head(tmp_path):
    # two instances stand in for two processes: B's append makes A's cached _last_hash stale
    path = tmp_path / "audit.jsonl"
    a, b = PermissionRAG(audit_path=path), PermissionRAG(audit_path=path)
    a.retrieve("q", USER)
    b.retrieve("q", USER)
    a.retrieve("q", USER)
    assert PermissionRAG.audit_file_failure(path) is None
    assert len(path.read_text().splitlines()) == 3


def test_truncate_then_keep_using_then_verify_still_fails(tmp_path):
    path = tmp_path / "audit.jsonl"
    rag = PermissionRAG(audit_path=path)
    for _ in range(3):
        rag.retrieve("q", USER)
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:-1]))  # attacker drops the newest entry
    with pytest.raises(RuntimeError, match="checkpoint"):
        rag.retrieve("q", USER)  # must not chain onto the doctored file
    with pytest.raises(RuntimeError, match="checkpoint"):
        rag.read_chunk("d#0", USER)  # every audited path refuses, not just retrieve
    assert path.read_text() == "".join(lines[:-1])
    assert PermissionRAG.audit_file_failure(path) is not None


def test_checkpoint_removed_mid_run_refuses_to_append(tmp_path):
    path = tmp_path / "audit.jsonl"
    rag = PermissionRAG(audit_path=path)
    rag.retrieve("q", USER)
    PermissionRAG.audit_head_path(path).unlink()
    with pytest.raises(RuntimeError, match="checkpoint"):
        rag.retrieve("q", USER)
    assert len(path.read_text().splitlines()) == 1


def test_stuck_lock_holder_times_out_instead_of_hanging(tmp_path, monkeypatch):
    import fcntl

    path = tmp_path / "audit.jsonl"
    rag = PermissionRAG(audit_path=path)
    rag.retrieve("q", USER)
    monkeypatch.setattr(PermissionRAG, "LOCK_TIMEOUT_S", 0.3)
    with open(f"{path}.lock", "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            rag.retrieve("q", USER)
        with pytest.raises(TimeoutError):
            rag.audit_failure()
        assert time.monotonic() - t0 < 5
    rag.retrieve("q", USER)  # a timeout is not tampering: it does not latch
    assert PermissionRAG.audit_file_failure(path) is None
    assert len(path.read_text().splitlines()) == 2


def test_writer_waits_while_another_process_holds_the_lock(tmp_path):
    # a holder stands in for a writer between "line appended" and "checkpoint written"
    import fcntl

    path = tmp_path / "audit.jsonl"
    rag = PermissionRAG(audit_path=path)
    rag.retrieve("q", USER)
    ctx = multiprocessing.get_context("spawn")
    with open(f"{path}.lock", "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        proc = ctx.Process(target=_worker, args=(str(path), "late", 1))
        proc.start()
        time.sleep(1.0)
        assert proc.is_alive()  # blocked on our lock, not racing past it
        fcntl.flock(holder, fcntl.LOCK_UN)
    proc.join(60)
    assert proc.exitcode == 0
    assert rag.audit_failure() is None


def test_lock_file_is_a_sibling_not_the_log(tmp_path):
    path = tmp_path / "audit.jsonl"
    PermissionRAG(audit_path=path).retrieve("q", USER)
    assert pathlib.Path(f"{path}.lock").exists()
    assert PermissionRAG.audit_file_failure(path) is None
