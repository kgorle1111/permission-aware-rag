"""Standalone toy retriever: no application or harness imports.

Replace this implementation with calls to your own index/session and return IDs
and scores before generation. This example uses term overlap, not BM25.
"""

import re


class ExampleAdapter:
    def build(self, chunks):
        self.chunks = []
        for chunk in chunks:
            for level in ("acl_doc", "acl_section", "acl_para"):
                value = chunk[level]
                if (
                    not isinstance(value, list)
                    or not value
                    or any(
                        not isinstance(p, str) or not re.fullmatch(r"\*|(?:user|group):[^,\s]+", p)
                        for p in value
                    )
                ):
                    raise ValueError("invalid ACL")
            self.chunks.append({**chunk, "words": set(re.findall(r"[a-z0-9]+", chunk["text"].lower()))})

    def retrieve(self, query, principal, k):
        # In a remote adapter, select credentials for the fixture identity here;
        # do not let the query itself supply a production identity or groups.
        scope = {"*", "user:" + principal["id"], *("group:" + g for g in principal["groups"])}
        words = set(re.findall(r"[a-z0-9]+", query.lower()))
        hits = []
        for chunk in self.chunks:
            if not all(scope.intersection(chunk[level]) for level in ("acl_doc", "acl_section", "acl_para")):
                continue
            score = len(words.intersection(chunk["words"])) / max(1, len(words))
            if score:
                hits.append({"id": chunk["id"], "score": score})
        return sorted(hits, key=lambda h: (-h["score"], h["id"]))[:k]

    def replace_acl(self, chunk_id, levels):
        for chunk in self.chunks:
            if chunk["id"] == chunk_id:
                chunk.update(levels)
                return
        raise ValueError("unknown chunk")


def factory():
    return ExampleAdapter()
