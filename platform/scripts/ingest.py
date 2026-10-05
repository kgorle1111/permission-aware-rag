"""Ingest the corpus: chunk, embed locally, inherit ACLs, index.

  python scripts/ingest.py            # corpus/docs.json
  python scripts/ingest.py path.json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ROOT  # noqa: E402
from app.ingest import ingest_corpus  # noqa: E402
from app.store import init_db  # noqa: E402

if __name__ == "__main__":
    init_db()
    path = sys.argv[1] if len(sys.argv) > 1 else ROOT / "corpus" / "docs.json"
    n = ingest_corpus(path)
    print(f"ingested {n} chunks from {path} (ACLs inherited per chunk)")
