"""Permission-Aware RAG: retrieval that enforces per-user document ACLs at query time.

Core security property: a chunk the caller cannot read is excluded BEFORE ranking
(pre-filtering), so its content can never influence scores, results, or citations.

ACL entries: "user:<id>", "group:<name>", or "*" (public).
"""
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
        if self.audit_path and self.audit_path.exists():
            with self.audit_path.open() as f:
                self.audit = [json.loads(line) for line in f if line.strip()]

    def add_document(self, doc_id, text, acl, chunk_words=80):
        """Ingest a document. `acl` is the set of principals allowed to read it."""
        if not acl:
            raise ValueError("acl must not be empty — refusing to ingest unreadable/ambiguous document")
        if any(c["doc_id"] == doc_id for c in self.chunks):
            raise ValueError(f"doc_id {doc_id!r} already ingested — remove/re-ingest is not supported yet")
        acl = set(acl)
        words = text.split()
        for i in range(0, len(words), chunk_words):
            chunk_text = " ".join(words[i:i + chunk_words])
            tf = Counter(tokenize(chunk_text))
            self.chunks.append({
                "id": f"{doc_id}#{i // chunk_words}",
                "doc_id": doc_id,
                "text": chunk_text,
                "acl": acl,
                "tf": tf,
            })

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
            self.audit.append(entry)
            if len(self.audit) > self.AUDIT_MAX:
                del self.audit[:-self.AUDIT_MAX]
            if self.audit_path:
                with self.audit_path.open("a") as f:
                    f.write(json.dumps(entry) + "\n")
        return results
