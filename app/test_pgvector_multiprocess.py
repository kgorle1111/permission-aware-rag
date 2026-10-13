"""Several app processes on one host share one audit checkpoint (T12, ROADMAP B17).

Skipped unless DATABASE_URL is set (see test_pgvector.py) and on Windows (the anchor lock is
fcntl.flock, so multi-process anchoring is POSIX-only).
"""

import multiprocessing as mp
import os
import pathlib
import sys
import threading

import pytest

psycopg = pytest.importorskip("psycopg")
ADMIN = os.environ.get("DATABASE_URL")
if not ADMIN:
    pytest.skip("DATABASE_URL not set; pgvector backend not under test", allow_module_level=True)
if sys.platform == "win32":
    pytest.skip("the multi-process anchor lock is POSIX-only (fcntl.flock)", allow_module_level=True)

import fcntl  # noqa: E402

import test_pgvector as base  # noqa: E402
from pgvector_rag import PgVectorRAG  # noqa: E402

GUEST = {"id": "guest", "groups": []}
READS, WORKERS = 150, 3


def _app_dsn() -> str:
    assert ADMIN
    return str(psycopg.conninfo.make_conninfo(ADMIN, user="rag_app", password="rag_app"))


def reader_process(key_hex: str, checkpoint: str, reads: int) -> None:
    """A second app process: its own connection and PgVectorRAG, the same checkpoint file."""
    rag = PgVectorRAG(_app_dsn(), principal_key=bytes.fromhex(key_hex), audit_checkpoint_path=checkpoint)
    for _ in range(reads):
        rag.retrieve("vacation", GUEST)


def _spawn_readers(rag: PgVectorRAG, checkpoint: pathlib.Path) -> list[mp.process.BaseProcess]:
    ctx = mp.get_context("spawn")
    procs: list[mp.process.BaseProcess] = [
        ctx.Process(target=reader_process, args=(rag._principal_key.hex(), str(checkpoint), READS))
        for _ in range(WORKERS)
    ]
    for p in procs:
        p.start()
    return procs


def _join(procs: list[mp.process.BaseProcess]) -> None:
    for p in procs:
        p.join(120)
        assert p.exitcode == 0


def test_processes_interleaving_retrieves_leave_a_verifying_checkpoint(tmp_path):
    cp = tmp_path / "audit.head"
    rag = base._fresh(cp)
    rag.retrieve("vacation", GUEST)
    _join(_spawn_readers(rag, cp))
    assert rag.audit_failure() is None
    assert cp.read_text().split()[0] == str(WORKERS * READS + 1)  # the file holds the true newest row


def test_verify_never_reports_tampering_while_other_processes_write(tmp_path):
    cp = tmp_path / "audit.head"
    rag = base._fresh(cp)
    rag.retrieve("vacation", GUEST)
    procs = _spawn_readers(rag, cp)
    bad = []
    while any(p.is_alive() for p in procs):
        if (msg := rag.audit_failure()) is not None:
            bad.append(msg)
    _join(procs)
    assert not bad, bad[:1]


def test_deleting_the_newest_row_still_fails_after_multi_process_traffic(tmp_path):
    cp = tmp_path / "audit.head"
    rag = base._fresh(cp)
    rag.retrieve("vacation", GUEST)
    _join(_spawn_readers(rag, cp))
    assert rag.audit_failure() is None
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM audit WHERE id = (SELECT max(id) FROM audit)")
    assert "tail rows deleted" in (rag.audit_failure() or "")


def test_a_later_retrieve_does_not_heal_a_deleted_tail(tmp_path):
    """The checkpoint only moves forward; otherwise the next audited call would paper over the deletion."""
    cp = tmp_path / "audit.head"
    rag = base._fresh(cp)
    for _ in range(3):
        rag.retrieve("vacation", GUEST)
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM audit WHERE id = (SELECT max(id) FROM audit)")
    rag.retrieve("vacation", GUEST)  # chains onto row 2, so the table now looks self-consistent
    assert "is past the checkpoint" in (rag.audit_failure() or "")
    assert cp.read_text().split()[0] == "3"


def test_a_stuck_lock_holder_fails_closed_instead_of_hanging(tmp_path):
    cp = tmp_path / "audit.head"
    rag = base._fresh(cp)
    rag.LOCK_TIMEOUT_S = 0.3
    with open(str(cp) + ".lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            rag.retrieve("vacation", GUEST)
        assert "timed out" in str(pytest.raises(TimeoutError, rag.audit_failure).value)
    rag.retrieve("vacation", GUEST)  # released: works again
    assert rag.audit_failure() is None


def test_two_threads_and_two_instances_share_the_lock(tmp_path):
    """Two PgVectorRAG objects in one process (as in tests or a pool) exclude each other too."""
    cp = tmp_path / "audit.head"
    a = base._fresh(cp)
    b = PgVectorRAG(_app_dsn(), principal_key=a._principal_key, audit_checkpoint_path=cp)
    try:
        threads = [
            threading.Thread(target=lambda r=r: [r.retrieve("vacation", GUEST) for _ in range(40)])
            for r in (a, b)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert a.audit_failure() is None and b.audit_failure() is None
    finally:
        b.conn.close()
