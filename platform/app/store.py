"""Postgres/SQLite: the two things that must never be partially written —
the ACL source of truth and the append-only audit log.

SQLite by default (zero infra, ACID for a single writer); DATABASE_URL for
Postgres when configured. config/postgres_rls.sql is an illustrative policy;
the runtime does not set the end-user context needed to enforce that policy.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
    update,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL
from .embeddings import fingerprint

Base = declarative_base()


class ChunkACL(Base):
    """Source of truth mirror of every chunk's ACL (Qdrant payload is the
    hot-path copy; this is the transactional record sync writes first)."""
    __tablename__ = "chunk_acl"

    chunk_id = Column(Integer, primary_key=True)
    doc_id = Column(String, index=True)
    acl = Column(JSON, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc), index=True)
    user_id = Column(String, index=True)
    groups = Column(JSON, default=list)
    query = Column(Text)
    returned_chunk_ids = Column(JSON, default=list)
    returned_doc_ids = Column(JSON, default=list)
    denied_count = Column(Integer, default=0)
    fail_closed = Column(Boolean, default=False)  # request denied due to an internal error


def make_engine(url: str = DATABASE_URL):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, pool_pre_ping=True, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class ChunkPolicy(Base):
    """Original policy inputs, retained independently of effective ACLs."""
    __tablename__ = "chunk_policy"
    chunk_id = Column(Integer, primary_key=True)
    doc_acl = Column(JSON, nullable=False)
    section_acl = Column(JSON, nullable=True)
    paragraph_acl = Column(JSON, nullable=True)


class SourceCheckpoint(Base):
    """Provider checkpoint committed atomically with effective permissions."""
    __tablename__ = "source_checkpoint"
    provider = Column(String, primary_key=True)
    token = Column(Text, nullable=False)


class PermissionMutation(Base):
    """Desired document grants persisted before remote payload mutations."""
    __tablename__ = "permission_mutation"
    doc_id = Column(String, primary_key=True)
    acl = Column(JSON, nullable=True)  # null deletion; [] denies


class PendingSourceCheckpoint(Base):
    """Provider cursor awaiting successful journal reconciliation."""
    __tablename__ = "pending_source_checkpoint"
    provider = Column(String, primary_key=True)
    token = Column(Text, nullable=False)


class PermissionState(Base):
    """Durable barrier: pending mutations block reads until reconciliation."""
    __tablename__ = "permission_state"
    id = Column(Integer, primary_key=True)
    revision = Column(Integer, nullable=False, default=0)
    pending = Column(Boolean, nullable=False, default=True)
    rebuild_required = Column(Boolean, nullable=False, default=True)


class ChunkText(Base):
    """Redacted chunk text mirrored for the keyword fallback (ACLs live in ChunkPolicy)."""
    __tablename__ = "chunk_text"
    chunk_id = Column(Integer, primary_key=True)
    text = Column(Text, nullable=False)


class IndexFingerprint(Base):
    """Embedding space the current index was built with."""
    __tablename__ = "index_fingerprint"
    id = Column(Integer, primary_key=True)
    backend = Column(String, nullable=False)
    model = Column(String, nullable=False)
    dim = Column(Integer, nullable=False)


def fingerprint_problem(session) -> str | None:
    """None when the index matches the configured embedder; else a prescriptive reason.

    A missing row means a legacy index: it fails closed until reingested.
    """
    row = session.get(IndexFingerprint, 1)
    if row is None:
        return "index has no embedding fingerprint; reingest"
    built, configured = (row.backend, row.model, row.dim), fingerprint()
    if built != configured:
        return (f"index built with {'/'.join(map(str, built))}; configured "
                f"{'/'.join(map(str, configured))}; reingest or restore config")
    return None


def init_db():
    existing = inspect(engine)
    prejournal = (existing.has_table(PermissionState.__tablename__) and
                  (not existing.has_table(PermissionMutation.__tablename__) or
                   not existing.has_table(PendingSourceCheckpoint.__tablename__)))
    if prejournal:
        # A prior release may have changed Qdrant before its SQL transaction
        # failed. Without document intents we cannot safely bound the replay.
        # Commit this barrier BEFORE creating tables: a crash during bootstrap
        # must not erase the fact that the pending mutation predates journals.
        with engine.begin() as conn:
            conn.execute(update(PermissionState)
                         .where(PermissionState.id == 1,
                                PermissionState.pending.is_(True),
                                PermissionState.rebuild_required.is_(False))
                         .values(pending=True, rebuild_required=True,
                                 revision=PermissionState.revision + 1))
    migrate = (existing.has_table("chunk_policy") and
               "paragraph_acl" not in {c["name"] for c in existing.get_columns("chunk_policy")})
    Base.metadata.create_all(engine)
    if migrate:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE chunk_policy ADD COLUMN paragraph_acl JSON"))
            conn.execute(update(PermissionState).values(pending=True, rebuild_required=True,
                                                       revision=PermissionState.revision + 1))
    with SessionLocal() as s:
        if s.get(PermissionState, 1) is None:
            s.add(PermissionState(id=1, revision=0, pending=True))
            s.commit()


@contextmanager
def permission_transaction():
    # UPDATE takes a write lock in SQLite and a row lock in Postgres. All
    # permission mutations and complete query/generation/audit operations use
    # this same row, including across workers sharing the SQL database.
    with SessionLocal() as s:
        result = s.execute(update(PermissionState).where(PermissionState.id == 1)
                           .values(revision=PermissionState.revision))
        if result.rowcount != 1:
            raise RuntimeError("permission state missing; initialize and ingest")
        state = s.get(PermissionState, 1)
        try:
            yield s, state
            s.commit()
        except Exception:
            s.rollback()
            raise
