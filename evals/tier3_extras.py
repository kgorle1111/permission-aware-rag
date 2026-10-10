"""Side measurements the ladder table cannot show (cache hit rate, abstain behaviour, chunk counts).

python3 evals/tier3_extras.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app")]

import run_evals  # noqa: E402
import underwriter_server as srv  # noqa: E402
from permission_rag import PermissionRAG  # noqa: E402
from rungs import AbstainRAG, SemanticCacheRAG, StructureChunkedRAG  # noqa: E402


def _load(rag):
    for doc_id, text, acl in srv.CORPUS:
        rag.add_document(doc_id, text, acl)
    return rag


def main() -> None:
    cache = _load(SemanticCacheRAG())
    for _ in range(5):
        for c in run_evals.CASES:
            cache.retrieve(c["q"], srv.USERS[c["role"]], k=run_evals.K)
    emb = cache._embed
    lookups = cache.cache_hits + cache.cache_misses
    print(
        f"semantic cache: {cache.cache_hits} hits / {lookups} lookups (5 repeats of {len(run_evals.CASES)} queries)"
    )
    print(f"embedding cache: {emb.hits} hits / {emb.hits + emb.misses} embeds")

    rag = _load(AbstainRAG())
    answerable = [c for c in run_evals.CASES if c["expect"]]
    hidden = [c for c in run_evals.CASES if not c["expect"]]

    def abstained(c):
        return rag.retrieve_with_status(c["q"], srv.USERS[c["role"]], k=run_evals.K)["abstained"]

    print(
        f"abstain @ {rag.threshold}: answered {sum(not abstained(c) for c in answerable)}/{len(answerable)} "
        f"answerable; abstained {sum(abstained(c) for c in hidden)}/{len(hidden)} no-expected-doc queries"
    )

    base, v2 = _load(PermissionRAG()), _load(StructureChunkedRAG())
    print(f"chunks on the demo corpus: baseline {len(base.chunks)}, structure-aware {len(v2.chunks)}")


if __name__ == "__main__":
    main()
