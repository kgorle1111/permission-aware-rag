from contextlib import contextmanager
from threading import Event, Thread, current_thread, local

import pytest

from app import sync
from app.store import PermissionState, SessionLocal
from conftest import reset_permission_source, reingest


def test_older_sync_intent_cannot_clear_newer_pending_barrier(ingested, monkeypatch):
    """A superseded pass must leave the newer pass's durable barrier in place."""
    reset_permission_source()
    real_transaction = sync.permission_transaction
    thread_state = local()
    older_barrier_committed = Event()
    newer_barrier_committed = Event()
    resume_older = Event()
    resume_newer = Event()
    errors = {}

    @contextmanager
    def coordinated_transaction():
        with real_transaction() as pair:
            yield pair

        count = getattr(thread_state, "transaction_count", 0) + 1
        thread_state.transaction_count = count
        if count != 1:
            return
        if current_thread().name == "older-sync":
            older_barrier_committed.set()
            assert resume_older.wait(10), "timed out waiting to resume older sync"
        elif current_thread().name == "newer-sync":
            newer_barrier_committed.set()
            assert resume_newer.wait(10), "timed out waiting to resume newer sync"

    monkeypatch.setattr(sync, "permission_transaction", coordinated_transaction)

    def run_sync(name):
        try:
            sync.sync_once()
        except Exception as exc:  # captured for assertions in the main test thread
            errors[name] = exc

    older = Thread(target=run_sync, args=("older",), name="older-sync")
    newer = Thread(target=run_sync, args=("newer",), name="newer-sync")
    try:
        older.start()
        assert older_barrier_committed.wait(10)

        newer.start()
        assert newer_barrier_committed.wait(10)

        # Both phase-one commits have happened. The newer pass now owns pending;
        # let the older pass attempt phase two while the newer one is paused.
        resume_older.set()
        older.join(10)
        assert not older.is_alive(), "older sync did not finish"
        assert "superseded" in str(errors.get("older", "")).lower()

        with SessionLocal() as session:
            assert session.get(PermissionState, 1).pending is True

        resume_newer.set()
        newer.join(10)
        assert not newer.is_alive(), "newer sync did not finish"
        assert "newer" not in errors
        with SessionLocal() as session:
            state = session.get(PermissionState, 1)
            assert state.pending is False
    finally:
        resume_older.set()
        resume_newer.set()
        if older.is_alive():
            older.join(10)
        if newer.is_alive():
            newer.join(10)
        reingest()
