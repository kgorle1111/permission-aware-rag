"""Ingestion: chunk + embed + inherit the document's ACL onto every chunk.

The chunk — not the document — is the unit that reaches the model, so the
chunk is the unit that carries permissions. Section-level overrides apply the
STRICTEST-WINS rule: a chunk spanning sensitivities gets the tighter ACL.
"""
from __future__ import annotations

import json
from pathlib import Path

from .embeddings import embed
from .store import ChunkACL, SessionLocal
from .vectorstore import reset_collection, upsert_chunks

OWNER_ONLY = ["user:__owner_only__"]


def strictest(acl_a: list[str], acl_b: list[str]) -> list[str]:
    """Strictest-wins ACL merge: a principal passes the result iff it passes
    BOTH inputs. '*' on one side means that side allows everyone, so the
    other side decides. An empty result collapses to owner-only (deny by
    default), never to public."""
    a, b = set(acl_a), set(acl_b)
    if "*" in a and "*" in b:
        return ["*"]
    if "*" in a:
        return sorted(b) or list(OWNER_ONLY)
    if "*" in b:
        return sorted(a) or list(OWNER_ONLY)
    return sorted(a & b) or list(OWNER_ONLY)


def validate_acl(acl: list[str]) -> list[str]:
    """Empty or malformed ACLs never enter the index — deny by default."""
    clean = [p for p in (acl or [])
             if p == "*" or p.startswith("user:") or p.startswith("group:")]
    return clean or list(OWNER_ONLY)


def chunk_document(doc: dict) -> list[dict]:
    """Split into paragraph chunks; each inherits doc ACL unless its section
    declares a stricter one."""
    doc_acl = validate_acl(doc.get("acl", []))
    chunks = []
    for section in doc["sections"]:
        acl = doc_acl
        if "acl" in section:
            acl = strictest(doc_acl, validate_acl(section["acl"]))
        for para in [p.strip() for p in section["text"].split("\n\n") if p.strip()]:
            chunks.append({"doc_id": doc["doc_id"], "text": para, "acl": acl})
    return chunks


def ingest_corpus(corpus_path: str | Path, reset: bool = True) -> int:
    docs = json.loads(Path(corpus_path).read_text())
    all_chunks = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc))
    for i, ch in enumerate(all_chunks):
        ch["id"] = i
    vectors = embed([c["text"] for c in all_chunks])
    for ch, v in zip(all_chunks, vectors):
        ch["vector"] = v

    if reset:
        reset_collection()
    upsert_chunks(all_chunks)

    # transactional source-of-truth mirror: fully applied or not at all
    with SessionLocal() as s:
        s.query(ChunkACL).delete()
        for ch in all_chunks:
            s.add(ChunkACL(chunk_id=ch["id"], doc_id=ch["doc_id"], acl=ch["acl"]))
        s.commit()
    return len(all_chunks)
