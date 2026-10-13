"""Map canonical fixture chunks onto the project's real BM25 reference retriever."""

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
from permission_rag import PermissionRAG  # noqa: E402


class ReferenceAdapter:
    def __init__(self, retriever_class: Any = PermissionRAG) -> None:
        self.retriever_class = retriever_class

    def build(self, chunks: list[dict[str, Any]]) -> None:
        self.rag = self.retriever_class()
        for chunk in chunks:
            self.rag.add_document(
                chunk["id"],
                "",
                chunk["acl_doc"],
                chunk_words=max(1, len(chunk["text"].split()) + 1),
                sections=[
                    {
                        "acl": chunk["acl_section"],
                        "paragraphs": [{"text": chunk["text"], "acl": chunk["acl_para"]}],
                    }
                ],
            )

    def retrieve(self, query: str, principal: dict[str, Any], k: int) -> list[dict[str, Any]]:
        return [{"id": r["doc_id"], "score": r["score"]} for r in self.rag.retrieve(query, principal, k)]

    def replace_acl(self, chunk_id: str, levels: dict[str, Any]) -> None:
        for chunk in self.rag.chunks:
            if chunk["doc_id"] == chunk_id:
                for level, value in levels.items():
                    chunk[level] = frozenset(value)
                chunk["acl"] = chunk["acl_doc"]
                return
        raise ValueError("unknown chunk")


def factory() -> ReferenceAdapter:
    return ReferenceAdapter()
