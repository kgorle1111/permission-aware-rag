"""Old flat-policy stores require reingestion before sync can restore readiness."""

import json

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from app import store, sync


def test_populated_old_sqlite_schema_marks_durable_rebuild_barrier(tmp_path, monkeypatch):
    engine = store.make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(store, "engine", engine)
    monkeypatch.setattr(store, "SessionLocal", sessions)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE chunk_policy (chunk_id INTEGER PRIMARY KEY, "
                              "doc_acl JSON NOT NULL, section_acl JSON)"))
            conn.execute(text("CREATE TABLE chunk_acl (chunk_id INTEGER PRIMARY KEY, "
                              "doc_id VARCHAR, acl JSON NOT NULL)"))
            conn.execute(text("CREATE TABLE permission_state (id INTEGER PRIMARY KEY, "
                              "revision INTEGER NOT NULL, pending BOOLEAN NOT NULL, "
                              "rebuild_required BOOLEAN NOT NULL)"))
            conn.execute(text("INSERT INTO chunk_policy VALUES (11, :doc, :section)"),
                         {"doc": json.dumps(["group:hr"]), "section": json.dumps(["user:bob"])})
            conn.execute(text("INSERT INTO chunk_acl VALUES (11, 'legacy', :acl)"),
                         {"acl": json.dumps([])})
            conn.execute(text("INSERT INTO permission_state VALUES (1, 7, 0, 0)"))

        store.init_db()
        assert "paragraph_acl" in {c["name"] for c in inspect(engine).get_columns("chunk_policy")}
        for _ in range(2):
            with sessions() as session:
                policy = session.get(store.ChunkPolicy, 11)
                assert policy.doc_acl == ["group:hr"]
                assert policy.section_acl == ["user:bob"]
                assert policy.paragraph_acl is None
                assert session.get(store.ChunkACL, 11).acl == []
                state = session.get(store.PermissionState, 1)
                assert (state.revision, state.pending, state.rebuild_required) == (8, True, True)
            store.init_db()

        source = tmp_path / "permissions.jsonl"
        source.write_text(json.dumps({"doc_id": "legacy", "acl": ["*"]}))
        with pytest.raises(RuntimeError, match="full reingestion required"):
            sync.sync_once(source)
        with sessions() as session:
            state = session.get(store.PermissionState, 1)
            assert state.pending is True
            assert state.rebuild_required is True
            assert state.revision == 9
            assert session.get(store.ChunkPolicy, 11).section_acl == ["user:bob"]
    finally:
        engine.dispose()
