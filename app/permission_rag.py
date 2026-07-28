"""Permission-Aware RAG: retrieval that enforces per-user document ACLs at query time.

Core security property: a chunk the caller cannot read is excluded BEFORE ranking
(pre-filtering), so its content can never influence scores, results, or citations.

ACL entries: "user:<id>", "group:<name>", or "*" (public).
"""
import math
import re
import time
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text):
    return _TOKEN.findall(text.lower())


class PermissionRAG:
    # ponytail: in-memory TF-IDF ranking, swap _score for embedding cosine when quality matters
    def __init__(self):
        self.chunks = []      # {id, doc_id, text, acl:set, tf:Counter}
        self.audit = []       # one entry per retrieve() call

    def add_document(self, doc_id, text, acl, chunk_words=80):
        """Ingest a document. `acl` is the set of principals allowed to read it."""
        if not acl:
            raise ValueError("acl must not be empty — refusing to ingest unreadable/ambiguous document")
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

    @staticmethod
    def _weights(tf, df, n):
        return {t: c * math.log(1 + n / df[t]) for t, c in tf.items() if t in df}

    def _score(self, qw, qnorm, chunk, df, n):
        cw = self._weights(chunk["tf"], df, n)
        dot = sum(w * cw.get(t, 0.0) for t, w in qw.items())
        norm = qnorm * math.sqrt(sum(w * w for w in cw.values()))
        return dot / norm if norm else 0.0

    def retrieve(self, query, user, k=3):
        """Return top-k chunks the user is allowed to read, ranked by relevance.

        Pre-filter, then rank: denied chunks are never scored, so their content
        cannot leak through relative scores or result ordering.
        """
        visible = [c for c in self.chunks if self.can_read(user, c["acl"])]
        denied = len(self.chunks) - len(visible)
        # IDF over the visible set only: a hidden doc must not shift visible scores
        df = Counter()
        for c in visible:
            df.update(set(c["tf"]))
        n = len(visible)
        qw = self._weights(Counter(tokenize(query)), df, n)
        qnorm = math.sqrt(sum(w * w for w in qw.values()))
        scored = sorted(((self._score(qw, qnorm, c, df, n), c) for c in visible),
                        key=lambda sc: sc[0], reverse=True)
        results = [
            {"id": c["id"], "doc_id": c["doc_id"], "text": c["text"],
             "score": round(s, 4)}
            for s, c in scored[:k]
            if s > 0
        ]
        self.audit.append({
            "ts": time.time(),
            "user": user["id"],
            "query": query,
            "returned": [r["id"] for r in results],
            "denied_chunks": denied,
        })
        return results
