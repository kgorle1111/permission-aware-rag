"""Postgres/SQLite: the two things that must never be partially written —
the ACL source of truth and the append-only audit log.

SQLite by default (zero infra, ACID for a single writer); DATABASE_URL for
Postgres in production. config/postgres_rls.sql adds Row-Level Security as
defense in depth there.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

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
    ts = Column(DateTime, default=dt.datetime.utcnow, index=True)
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


def init_db():
    Base.metadata.create_all(engine)
