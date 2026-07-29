"""Permission-Aware RAG: retrieval that enforces per-user document ACLs at query time.

Core security property: a chunk the caller cannot read is excluded BEFORE ranking
(pre-filtering), so its content can never influence scores, results, or citations.

ACL entries: "user:<id>", "group:<name>", or "*" (public).
"""
import hashlib
import json
import math
import pathlib
import re
import threading
import time
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text):
    return _TOKEN.findall(text.lower())


class PermissionRAG:
    # ponytail: in-memory BM25 ranking, swap _score for embedding cosine when recall matters
    AUDIT_MAX = 1000  # in-memory bound; the JSONL file keeps full history
    def __init__(self, audit_path=None):
        """audit_path: optional JSONL file; entries append there and reload on start,
        so the trail survives restarts (compliance requirement, not a nice-to-have)."""
        self.chunks = []      # {id, doc_id, text, acl:set, tf:Counter}
        self.audit = []       # one entry per retrieve() call
        self.audit_path = pathlib.Path(audit_path) if audit_path else None
        self._audit_lock = threading.Lock()  # servers run threaded; keep JSONL lines whole
        self._last_hash = ""  # tamper-evident chain: each entry carries prev line's sha256
        if self.audit_path and self.audit_path.exists():
            with self.audit_path.open() as f:
                lines = [line.rstrip("\n") for line in f if line.strip()]
            self.audit = [json.loads(line) for line in lines]
            if lines:
                self._last_hash = hashlib.sha256(lines[-1].encode()).hexdigest()

    def add_document(self, doc_id, text, acl, chunk_words=80):
        """Ingest a document. `acl` is the set of principals allowed to read it."""
        if not acl:
            raise ValueError("acl must not be empty — refusing to ingest unreadable/ambiguous document")
        if any(c["doc_id"] == doc_id for c in self.chunks):
            raise ValueError(f"doc_id {doc_id!r} already ingested — use remove_document() then re-add")
        acl = set(acl)
        for i, chunk_text in enumerate(self._chunk_texts(text, chunk_words)):
            self.chunks.append({
                "id": f"{doc_id}#{i}",
                "doc_id": doc_id,
                "text": chunk_text,
                "acl": acl,
                "tf": Counter(tokenize(chunk_text)),
            })

    @staticmethod
    def _chunk_texts(text, chunk_words):
        """Pack whole sentences up to chunk_words, with a one-sentence overlap so a
        fact spanning a boundary stays retrievable. Oversize sentences hard-split."""
        pieces = []
        for s in re.split(r"(?<=[.!?])\s+", text.strip()):
            words = s.split()
            if len(words) > chunk_words:
                pieces.extend(" ".join(words[i:i + chunk_words])
                              for i in range(0, len(words), chunk_words))
            elif words:
                pieces.append(s)
        chunks, cur, cur_len = [], [], 0
        for s in pieces:
            n = len(s.split())
            if cur and cur_len + n > chunk_words:
                chunks.append(" ".join(cur))
                last = cur[-1]
                # overlap only if the carried sentence leaves room for new content
                cur, cur_len = ([last], len(last.split())) if len(last.split()) <= chunk_words // 2 else ([], 0)
            cur.append(s)
            cur_len += n
        if cur:
            chunks.append(" ".join(cur))
        return chunks

    def remove_document(self, doc_id):
        """Remove all chunks for doc_id; returns count removed. Re-ingest = remove + add.
        df/n are computed per-query over the visible set, so no index correction needed."""
        before = len(self.chunks)
        self.chunks = [c for c in self.chunks if c["doc_id"] != doc_id]
        return before - len(self.chunks)

    @staticmethod
    def can_read(user, acl):
        """user = {"id": str, "groups": [str, ...]}"""
        if "*" in acl:
            return True
        if f"user:{user['id']}" in acl:
            return True
        return any(f"group:{g}" in acl for g in user.get("groups", ()))

    # BM25: term saturation (k1) stops one repeated word dominating; length
    # normalization (b) stops long chunks winning on bulk alone.
    K1, B = 1.5, 0.75

    def _score(self, qtokens, chunk, df, n, avglen):
        length = sum(chunk["tf"].values())
        score = 0.0
        for t in qtokens:
            f = chunk["tf"].get(t)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * length / avglen))
        return score

    def retrieve(self, query, user, k=3):
        """Return top-k chunks the user is allowed to read, ranked by relevance.

        Pre-filter, then rank: denied chunks are never scored, so their content
        cannot leak through relative scores or result ordering.
        """
        t0 = time.perf_counter()
        visible = [c for c in self.chunks if self.can_read(user, c["acl"])]
        denied = len(self.chunks) - len(visible)
        # IDF over the visible set only: a hidden doc must not shift visible scores
        df = Counter()
        for c in visible:
            df.update(set(c["tf"]))
        n = len(visible)
        avglen = sum(sum(c["tf"].values()) for c in visible) / n if n else 1.0
        qtokens = set(tokenize(query))
        scored = sorted(((self._score(qtokens, c, df, n, avglen), c) for c in visible),
                        key=lambda sc: sc[0], reverse=True)
        results = [
            {"id": c["id"], "doc_id": c["doc_id"], "text": c["text"],
             "score": round(s, 4)}
            for s, c in scored[:k]
            if s > 0
        ]
        entry = {
            "ts": time.time(),
            "user": user["id"],
            "query": query,
            "returned": [r["id"] for r in results],
            "denied_chunks": denied,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
        with self._audit_lock:
            entry["prev_sha256"] = self._last_hash
            line = json.dumps(entry)
            self._last_hash = hashlib.sha256(line.encode()).hexdigest()
            self.audit.append(entry)
            if len(self.audit) > self.AUDIT_MAX:
                del self.audit[:-self.AUDIT_MAX]
            if self.audit_path:
                with self.audit_path.open("a") as f:
                    f.write(line + "\n")
        return results

    @staticmethod
    def verify_audit_chain(path):
        """True iff the JSONL audit log's hash chain is intact (no edited/removed lines)."""
        prev = ""
        with pathlib.Path(path).open() as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                if json.loads(line).get("prev_sha256") != prev:
                    return False
                prev = hashlib.sha256(line.encode()).hexdigest()
        return True
