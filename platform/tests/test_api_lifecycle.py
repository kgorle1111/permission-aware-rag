"""API lifespan and request-boundary behavior."""
import asyncio
import json
import threading
from pathlib import Path

from conftest import auth, mint, reset_permission_source


def test_lifespan_polls_permissions_and_joins_inflight_sync(client, monkeypatch):
    """The startup poll applies source changes and shutdown waits for it."""
    from app import config
    from app import main
    from app.store import ChunkACL, SessionLocal
    from app.sync import sync_once as apply_sync

    reset_permission_source()
    source_path = Path(config.PERMISSIONS_SOURCE)
    source = [json.loads(line) for line in source_path.read_text().splitlines() if line.strip()]
    for row in source:
        if row["doc_id"] == "hr-salaries":
            row["acl"] = []
            break
    source_path.write_text("\n".join(json.dumps(row) for row in source) + "\n")

    monkeypatch.setattr(config, "SYNC_INTERVAL_S", 0.01)
    sync_started = threading.Event()
    allow_sync_to_finish = threading.Event()
    calls = []

    def controlled_sync():
        result = apply_sync()
        calls.append(result)
        # The lifespan reconciles once before accepting traffic, then starts
        # the periodic poll. Hold that poll until shutdown begins.
        if len(calls) == 2:
            sync_started.set()
            allow_sync_to_finish.wait(timeout=5)
        return result

    monkeypatch.setattr(main, "sync_once", controlled_sync)

    async def exercise_lifespan():
        lifespan = main.app.router.lifespan_context(main.app)
        await lifespan.__aenter__()
        observed = await asyncio.to_thread(sync_started.wait, 3)
        assert observed, "periodic poll did not run after startup reconciliation"

        with SessionLocal() as session:
            row = session.query(ChunkACL).filter_by(doc_id="hr-salaries").first()
            assert row is not None
            assert row.acl == []

        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not shutdown.done(), "shutdown abandoned the active sync call"
        allow_sync_to_finish.set()
        await asyncio.wait_for(shutdown, timeout=3)

    try:
        asyncio.run(exercise_lifespan())
    finally:
        allow_sync_to_finish.set()
        reset_permission_source()
        # Restore the fixture's original ACL/index state for later tests.
        from conftest import reingest
        reingest()

    assert len(calls) == 2


def test_sync_authorization_failure_and_request_validation(client, monkeypatch):
    from app import config
    from app import main

    assert client.post("/sync").status_code == 401
    assert client.post("/sync", headers=auth("alice")).status_code == 403

    def fail_sync():
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(main, "sync_once", fail_sync)
    security_token = mint("sec@company.com", ["security"])
    response = client.post("/sync", headers={"Authorization": f"Bearer {security_token}"})
    assert response.status_code == 503

    assert client.post("/query", json={"query": "   "}, headers=auth("alice")).status_code == 422
    monkeypatch.setattr(config, "DEMO_MODE", False)
    assert client.get("/demo/tokens").status_code == 404
