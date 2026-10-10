"""Permission-Aware RAG: retrieval that enforces per-user document ACLs at query time.

Core security property: a chunk the caller cannot read is excluded BEFORE ranking
(pre-filtering), so its content can never influence scores, results, or citations.

ACL entries: "user:<id>", "group:<name>", or "*" (public).
"""

import hashlib
import json
import math
import os
import pathlib
import re
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterable

_TOKEN = re.compile(r"[a-z0-9]+")
# no comma: the pgvector backend joins principals with ',' for the RLS policy.
# Use fullmatch, never match with ^...$ — '$' also matches before a trailing '\n'.
_ACL_ENTRY = re.compile(r"\*|(user|group):[^,\s]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def normalize_acl(acl: Iterable[str]) -> frozenset[str]:
    """Validate an ACL at the ingest boundary; every backend must route through this."""
    if isinstance(acl, (str, bytes)):
        raise TypeError('acl must be a set/list of entries, e.g. {"group:hr"}, not a bare string')
    entries = frozenset(acl)
    if not entries:
        raise ValueError("acl must not be empty — refusing to ingest unreadable/ambiguous document")
    for e in entries:
        if not isinstance(e, str) or not _ACL_ENTRY.fullmatch(e):
            raise ValueError(f'bad acl entry {e!r}: expected "*", "user:<id>" or "group:<name>"')
    return entries


class PermissionRAG:
    # ponytail: in-memory BM25 ranking, swap _score for embedding cosine when recall matters
    AUDIT_MAX = 1000  # in-memory bound; the JSONL file keeps full history

    def __init__(self, audit_path: str | pathlib.Path | None = None) -> None:
        """audit_path: optional JSONL file; entries append there and reload on start,
        so the trail survives restarts (compliance requirement, not a nice-to-have)."""
        self.chunks = []  # {id, doc_id, text, acl:set, tf:Counter}
        self.audit = []  # one entry per retrieve() call
        self.audit_path = pathlib.Path(audit_path) if audit_path else None
        self._audit_lock = threading.Lock()  # servers run threaded; keep JSONL lines whole
        self._last_hash = ""  # tamper-evident chain: each entry carries prev line's sha256
        self._audit_failed = False
        if self.audit_path and (self.audit_path.exists() or self.audit_head_path(self.audit_path).exists()):
            if not self.verify_audit_chain(self.audit_path):
                raise ValueError("audit log or head checkpoint is missing, corrupt, or unanchored")
        if self.audit_path and self.audit_path.exists():
            with self.audit_path.open() as f:
                lines = [line.rstrip("\n") for line in f if line.strip()]
            self.audit = [json.loads(line) for line in lines]
            if lines:
                self._last_hash = hashlib.sha256(lines[-1].encode()).hexdigest()

    def add_document(
        self,
        doc_id: str,
        text: str,
        acl: Iterable[str],
        chunk_words: int = 80,
        *,
        sections: list[dict] | None = None,
    ) -> None:
        """Ingest atomically; each optional section/paragraph ACL narrows its parents."""
        chunks = self.document_chunks(text, acl, chunk_words, sections=sections)
        if any(c["doc_id"] == doc_id for c in self.chunks):
            raise ValueError(f"doc_id {doc_id!r} already ingested — use remove_document() then re-add")
        for i, chunk in enumerate(chunks):
            self.chunks.append(
                {
                    "id": f"{doc_id}#{i}",
                    "doc_id": doc_id,
                    **chunk,
                    "tf": Counter(tokenize(chunk["text"])),
                }
            )

    @staticmethod
    def document_chunks(text, acl, chunk_words=80, *, sections=None):
        """Validate all boundaries before ingest; overlap never crosses a boundary.

        Missing child ACL means unrestricted at that level; an explicitly empty
        or malformed ACL is rejected rather than silently becoming public.
        """
        doc_acl = normalize_acl(acl)
        if not isinstance(text, str) or not isinstance(chunk_words, int) or chunk_words < 1:
            raise ValueError("text must be a string and chunk_words a positive integer")
        if sections is not None and not isinstance(sections, list):
            raise TypeError("sections must be a list")
        chunks = []

        def pack(value, section_acl, paragraph_acl):
            if not isinstance(value, str):
                raise TypeError("paragraph text must be a string")
            for part in PermissionRAG._chunk_texts(value, chunk_words):
                chunks.append(
                    {
                        "text": part,
                        "acl": doc_acl,
                        "acl_doc": doc_acl,
                        "acl_section": section_acl,
                        "acl_para": paragraph_acl,
                    }
                )

        pack(text, frozenset({"*"}), frozenset({"*"}))
        for section in sections or []:
            if not isinstance(section, dict):
                raise TypeError("section must be an object")
            section_acl = normalize_acl(section["acl"]) if "acl" in section else frozenset({"*"})
            if "paragraphs" in section:
                if "text" in section or not isinstance(section["paragraphs"], list):
                    raise ValueError("section requires either text or a paragraphs list")
                for para in section["paragraphs"]:
                    if not isinstance(para, dict):
                        raise TypeError("paragraph must be an object")
                    para_acl = normalize_acl(para["acl"]) if "acl" in para else frozenset({"*"})
                    pack(para.get("text"), section_acl, para_acl)
            else:
                pack(section.get("text"), section_acl, frozenset({"*"}))
        return chunks

    def can_read_chunk(self, user, chunk):
        return all(self.can_read(user, chunk[level]) for level in ("acl_doc", "acl_section", "acl_para"))

    @staticmethod
    def _chunk_texts(text: str, chunk_words: int) -> list[str]:
        """Pack whole sentences up to chunk_words, with a one-sentence overlap so a
        fact spanning a boundary stays retrievable. Oversize sentences hard-split."""
        pieces = []
        for s in re.split(r"(?<=[.!?])\s+", text.strip()):
            words = s.split()
            if len(words) > chunk_words:
                pieces.extend(" ".join(words[i : i + chunk_words]) for i in range(0, len(words), chunk_words))
            elif words:
                pieces.append(s)
        chunks, cur, cur_len = [], [], 0
        for s in pieces:
            n = len(s.split())
            if cur and cur_len + n > chunk_words:
                chunks.append(" ".join(cur))
                last = cur[-1]
                # overlap only if the carried sentence leaves room for new content
                cur, cur_len = (
                    ([last], len(last.split())) if len(last.split()) <= chunk_words // 2 else ([], 0)
                )
            cur.append(s)
            cur_len += n
        if cur:
            chunks.append(" ".join(cur))
        return chunks

    def remove_document(self, doc_id: str) -> int:
        """Remove all chunks for doc_id; returns count removed. Re-ingest = remove + add.
        df/n are computed per-query over the visible set, so no index correction needed."""
        before = len(self.chunks)
        self.chunks = [c for c in self.chunks if c["doc_id"] != doc_id]
        return before - len(self.chunks)

    @staticmethod
    def can_read(user: dict, acl: set[str]) -> bool:
        """user = {"id": str, "groups": [str, ...]}"""
        if "*" in acl:
            return True
        if f"user:{user['id']}" in acl:
            return True
        return any(f"group:{g}" in acl for g in user.get("groups", ()))

    # BM25: term saturation (k1) stops one repeated word dominating; length
    # normalization (b) stops long chunks winning on bulk alone.
    K1, B = 1.5, 0.75

    def _score(self, qtokens: set[str], chunk: dict, df: Counter, n: int, avglen: float) -> float:
        length = sum(chunk["tf"].values())
        score = 0.0
        for t in qtokens:
            f = chunk["tf"].get(t)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * length / avglen))
        return score

    def retrieve(self, query: str, user: dict, k: int = 3) -> list[dict]:
        """Return top-k chunks the user is allowed to read, ranked by relevance.

        Pre-filter, then rank: denied chunks are never scored, so their content
        cannot leak through relative scores or result ordering.
        """
        t0 = time.perf_counter()
        if self._audit_failed:
            raise RuntimeError("audit persistence failed; recovery required")
        visible = [c for c in self.chunks if self.can_read_chunk(user, c)]
        denied = len(self.chunks) - len(visible)
        # IDF over the visible set only: a hidden doc must not shift visible scores
        df = Counter()
        for c in visible:
            df.update(set(c["tf"]))
        n = len(visible)
        avglen = sum(sum(c["tf"].values()) for c in visible) / n if n else 1.0
        qtokens = set(tokenize(query))
        scored = sorted(
            ((self._score(qtokens, c, df, n, avglen), c) for c in visible), key=lambda sc: sc[0], reverse=True
        )
        results = [
            {"id": c["id"], "doc_id": c["doc_id"], "text": c["text"], "score": round(s, 4)}
            for s, c in scored[:k]
            if s > 0
        ]
        entry = {
            "ts": time.time(),
            "user": user["id"],
            "query": "[redacted]",
            "returned": [r["id"] for r in results],
            "denied_chunks": denied,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
        self._append_audit(entry)
        return results

    def read_chunk(self, chunk_id: str, user: dict) -> dict | None:
        """One chunk by id if `user` passes every ACL level, else None (same for missing ids).

        Audited like a search, and the audit write happens before any text is returned.
        """
        if self._audit_failed:
            raise RuntimeError("audit persistence failed; recovery required")
        chunk = next((c for c in self.chunks if c["id"] == chunk_id), None)
        readable = chunk is not None and self.can_read_chunk(user, chunk)
        self._append_audit(
            {
                "ts": time.time(),
                "user": user["id"],
                "op": "get_chunk",
                "returned": [chunk_id] if readable else [],
            }
        )
        return chunk if readable else None

    def _append_audit(self, entry: dict) -> None:
        with self._audit_lock:
            if self._audit_failed:
                raise RuntimeError("audit persistence failed; recovery required")
            entry["prev_sha256"] = self._last_hash
            line = json.dumps(entry)
            head = hashlib.sha256(line.encode()).hexdigest()
            if self.audit_path:
                try:
                    with self.audit_path.open("a") as f:
                        f.write(line + "\n")
                        f.flush()
                        os.fsync(f.fileno())
                    self._write_audit_head(head)
                except OSError:
                    self._audit_failed = True
                    raise
            self._last_hash = head
            self.audit.append(entry)
            if len(self.audit) > self.AUDIT_MAX:
                del self.audit[: -self.AUDIT_MAX]

    @staticmethod
    def audit_head_path(path: str | pathlib.Path) -> pathlib.Path:
        return pathlib.Path(str(path) + ".head")

    def _write_audit_head(self, head: str) -> None:
        """Replace the local checkpoint only after the append has been flushed.

        A crash between the two writes fails closed on restart. This checkpoint
        detects tail edits/truncation, but an attacker who rewrites BOTH files
        requires an independently retained expected_head to be detected.
        """
        target = self.audit_head_path(self.audit_path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as f:
                temporary = pathlib.Path(f.name)
                f.write(head)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, target)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    @staticmethod
    def verify_audit_chain(path: str | pathlib.Path, expected_head: str | None = None) -> bool:
        """Verify every line AND the head; supply an external head for stronger evidence.

        Without expected_head, require the local .head checkpoint. Legacy logs
        without a checkpoint are unanchored and cannot be silently trusted.
        """
        prev = ""
        try:
            if expected_head is None:
                expected_head = PermissionRAG.audit_head_path(path).read_text()
            with pathlib.Path(path).open() as f:
                for line in f:
                    if not line.endswith("\n"):
                        return False
                    line = line[:-1]
                    entry = json.loads(line)
                    if not isinstance(entry, dict) or entry.get("prev_sha256") != prev:
                        return False
                    prev = hashlib.sha256(line.encode()).hexdigest()
        except (OSError, ValueError, UnicodeError):
            return False
        return prev == expected_head
