"""Ingestion: chunk + embed + inherit the document's ACL onto every chunk.

The chunk — not the document — is the unit that reaches the model, so the
chunk is the unit that carries permissions. Section-level overrides apply the
STRICTEST-WINS rule: a chunk spanning sensitivities gets the tighter ACL.
"""
from __future__ import annotations

import json
from pathlib import Path

from .embeddings import embed
from .store import ChunkACL, ChunkPolicy, SourceCheckpoint, permission_transaction
from .vectorstore import reset_collection, upsert_chunks

OWNER_ONLY = []  # empty means deny everyone; no forgeable sentinel principal


def strictest(acl_a: list[str], acl_b: list[str]) -> list[str]:
    """Conservative intersection of principal grants; wildcard yields the other ACL.

    An empty intersection denies everyone. Different memberships can satisfy
    the two inputs separately yet be denied by this flat-set representation.
    """
    a, b = set(acl_a), set(acl_b)
    if "*" in a and "*" in b:
        return ["*"]
    if "*" in a:
        return sorted(b) or list(OWNER_ONLY)
    if "*" in b:
        return sorted(a) or list(OWNER_ONLY)
    return sorted(a & b) or list(OWNER_ONLY)


def validate_acl(acl: list[str]) -> list[str]:
    """Malformed or empty ACLs deny everyone, including mixed valid/invalid lists."""
    if not isinstance(acl, list):
        return []
    clean = [p for p in acl if isinstance(p, str) and (
        p == "*" or (p.startswith(("user:", "group:")) and
                     bool(p.split(":", 1)[1].strip()))) ]
    return sorted(set(clean)) if len(clean) == len(acl) else []



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
            chunks.append({"doc_id": doc["doc_id"], "text": para, "acl": acl,
                           "doc_acl": doc_acl,
                           "section_acl": validate_acl(section["acl"]) if "acl" in section else None})
    return chunks


def ingest_corpus(corpus_path: str | Path, reset: bool = True) -> int:
    docs = json.loads(Path(corpus_path).read_text())
    if not isinstance(docs, list):
        raise ValueError("corpus must be a list of documents")
    seen = set()
    all_chunks = []
    for doc in docs:
        doc_id = doc.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id.strip() or doc_id in seen:
            raise ValueError("document ids must be nonempty and unique")
        seen.add(doc_id)
        all_chunks.extend(chunk_document(doc))
    for i, ch in enumerate(all_chunks):
        ch["id"] = i
    vectors = embed([c["text"] for c in all_chunks])
    for ch, v in zip(all_chunks, vectors):
        ch["vector"] = v

    if not reset:
        raise ValueError("incremental ingestion is unsupported; use a full replacement")
    # Persist the barrier before touching either store. A crash or failed write
    # leaves queries denied until a successful full reingestion.
    with permission_transaction() as (s, state):
        state.revision += 1
        intent = state.revision
        state.pending = True
        state.rebuild_required = True
    with permission_transaction() as (s, state):
        if state.revision != intent:
            raise RuntimeError("ingestion superseded; retry full reingestion")
        reset_collection()
        if all_chunks:
            upsert_chunks(all_chunks)
        s.query(ChunkACL).delete()
        s.query(ChunkPolicy).delete()
        s.query(SourceCheckpoint).delete()
        for ch in all_chunks:
            s.add(ChunkACL(chunk_id=ch["id"], doc_id=ch["doc_id"], acl=ch["acl"]))
            s.add(ChunkPolicy(chunk_id=ch["id"], doc_acl=ch["doc_acl"],
                              section_acl=ch["section_acl"]))
        state.revision += 1
        state.pending = False
        state.rebuild_required = False
    from .retrieval import clear_cache
    clear_cache()
    return len(all_chunks)
