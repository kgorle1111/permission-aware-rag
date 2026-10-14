"""F5.3: principal-signing key rotation (T18). Needs DATABASE_URL like test_pgvector.py.

rag_secret holds a current key and an optional previous key, each with a key id (kid). Tokens carry
the kid; Postgres accepts the current key, or the previous key only inside its grace window.
"""

import hashlib
import hmac
import logging
import time

import pgvector_rag as pg
import pytest
import test_pgvector as base  # skips this module too when DATABASE_URL is unset
from test_pgvector import BOB, GUEST, psycopg

ADMIN: str = base.ADMIN or ""  # base skips the module when unset


@pytest.fixture
def rag():
    """Fresh schema AND a fresh key table: rotation state must not leak into other test files."""

    def reset():
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS rag_secret")

    reset()
    r = base._fresh()
    base._seed(r)
    yield r
    reset()


def _app(kid, key):
    """A second app process configured with a specific key (as after a config change)."""
    dsn = psycopg.conninfo.make_conninfo(ADMIN, user="rag_app", password="rag_app")
    r = pg.PgVectorRAG(dsn, principal_key=key, principal_key_id=kid)
    base._open.append(r)
    return r


def _key() -> bytes:
    with psycopg.connect(ADMIN) as c:
        return bytes(c.execute("SELECT key FROM rag_secret WHERE id = 1").fetchone()[0])


def _ids(r, user=BOB, q="salary bands"):
    return {x["doc_id"] for x in r.retrieve(q, user)}


def test_tokens_name_their_key(rag):
    parts = rag.sign_principals(["*"]).split("|")
    assert len(parts) == 4 and parts[2] == "k1"
    assert _ids(rag) == {"salaries"}


def test_old_key_works_during_grace_and_fails_after_retire(rag):
    old_key = _key()
    old = _app("k1", old_key)
    new = pg.rotate_principal_key(ADMIN)
    assert new.kid == "k2" and new.key != old_key and len(new.key) >= 32
    fresh = _app(new.kid, new.key)
    assert _ids(old) == {"salaries"}, "previous key must keep working through the grace window"
    assert _ids(fresh) == {"salaries"}
    assert pg.retire_previous_key(ADMIN) is True
    assert _ids(old) == set(), "after retire the old key is rejected (fail closed: no rows)"
    assert _ids(fresh) == {"salaries"}
    assert pg.retire_previous_key(ADMIN) is False


def test_previous_key_expires_by_itself(rag):
    old = _app("k1", _key())
    pg.rotate_principal_key(ADMIN, grace_seconds=1)
    assert _ids(old) == {"salaries"}
    time.sleep(1.5)
    assert _ids(old) == set()


def test_grace_window_is_bounded():
    for bad in (0, -5, pg.MAX_GRACE_SECONDS + 1):
        with pytest.raises(ValueError):
            pg.rotate_principal_key(ADMIN, grace_seconds=bad)


def test_forged_tokens_are_rejected(rag):
    old_key = _key()
    new = pg.rotate_principal_key(ADMIN)
    principals = ["*", "group:hr"]
    now = time.time()
    unknown_kid = pg.sign_principals(new.key, principals, now, kid="k9")
    old_key_current_kid = pg.sign_principals(old_key, principals, now, kid=new.kid)
    new_key_previous_kid = pg.sign_principals(new.key, principals, now, kid="k1")
    good = pg.sign_principals(new.key, principals, now, kid=new.kid)
    swapped_kid = good.replace(f"|{new.kid}|", "|k1|")
    body = f"{','.join(principals)}|{int(now)}"  # the pre-F5.3 format: no kid
    legacy_three_part = f"{body}|{hmac.new(new.key, body.encode(), hashlib.sha256).hexdigest()}"
    for forged in (unknown_kid, old_key_current_kid, new_key_previous_kid, swapped_kid, legacy_three_part):
        assert _count_visible(forged) == 0, forged.split("|")[2:3]
    assert _count_visible(good) > 0


def _count_visible(token):
    with base._reader() as c:
        c.execute("SELECT set_config('rag.token', %s, true)", (token,))
        return c.execute("SELECT count(*) FROM chunks").fetchone()[0]


def test_app_role_cannot_read_keys(rag):
    pg.rotate_principal_key(ADMIN)
    for stmt in (
        "SELECT key FROM rag_secret",
        "SELECT kid FROM rag_secret",
        "SELECT count(*) FROM rag_secret",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rag.conn.transaction():
                rag.conn.execute(stmt)
    for stmt in ("UPDATE rag_secret SET key = '\\x00'", "DELETE FROM rag_secret"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rag.conn.transaction():
                rag.conn.execute(stmt)


def test_rotation_state_machine(rag):
    pg.rotate_principal_key(ADMIN)
    with pytest.raises(ValueError, match="retire_previous_key"):
        pg.rotate_principal_key(ADMIN)  # previous still in grace: refuse rather than cut it off
    assert pg.retire_previous_key(ADMIN) is True
    assert pg.rotate_principal_key(ADMIN).kid == "k3"  # key ids are never reused


def test_keys_never_reach_repr_or_logs(rag, caplog):
    with caplog.at_level(logging.DEBUG):
        new = pg.rotate_principal_key(ADMIN)
        pg.retire_previous_key(ADMIN)
    text = repr(new) + str(new) + "".join(r.getMessage() for r in caplog.records)
    assert new.key.hex() not in text and repr(new.key) not in text and new.kid in repr(new)


def test_setup_keeps_the_rotated_key_and_returns_the_current_one(rag):
    new = pg.rotate_principal_key(ADMIN)
    assert pg.setup_schema(ADMIN) == new.key  # re-running setup never rotates or reverts silently
    assert _ids(_app(new.kid, new.key)) == {"salaries"}


def test_legacy_single_key_table_migrates_to_k1():
    """A database created before F5.3 has rag_secret(id CHECK id=1, key) and no kid."""
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS rag_secret")
        c.execute("CREATE TABLE rag_secret (id integer PRIMARY KEY CHECK (id = 1), key bytea NOT NULL)")
        c.execute("INSERT INTO rag_secret VALUES (1, %s)", (b"L" * 32,))
    try:
        assert pg.setup_schema(ADMIN) == b"L" * 32
        r = base._fresh()
        assert r.sign_principals(["*"]).split("|")[2] == "k1"
        base._seed(r)
        assert _ids(r) == {"salaries"}
        assert pg.rotate_principal_key(ADMIN).kid == "k2"
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS rag_secret")


def test_key_id_is_validated_before_it_can_forge_token_structure():
    for bad in ("", "k1|x", "K1", "k", "1", "k1 "):
        with pytest.raises(ValueError):
            pg.sign_principals(b"x" * 32, ["*"], kid=bad)


def test_unrelated_user_still_sees_nothing_after_rotation(rag):
    pg.rotate_principal_key(ADMIN)
    assert "salaries" not in _ids(rag, GUEST, "salary bands")
