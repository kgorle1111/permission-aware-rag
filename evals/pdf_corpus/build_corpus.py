"""Extract every fetched PDF with platform/app/pdf_ingest.py into a corpus JSON.

Text is REAL (federal publications). ACLs are SYNTHETIC: one group per publisher,
assigned here only so the leak checks have something to violate.

usage: python build_corpus.py [MAX_PAGES_PER_DOC]   -> data/corpus.json, data/extract_stats.json
"""

from __future__ import annotations

import json
import sys
import time
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "platform"))

from app import pdf_ingest  # noqa: E402

GROUP = {
    "FDIC": "group:examiners",
    "IRS": "group:tax",
    "CFPB": "group:compliance",
    "HUD": "group:housing",
    "GAO": "group:policy",
    "Treasury": "group:policy",
    "FEMA": "group:flood",
    "Federal Register": "group:archive",
}


def work(args: tuple) -> dict:
    entry, max_pages = args
    t0 = time.time()
    try:
        return _extract(entry, max_pages, t0)
    except (
        Exception
    ) as exc:  # one bad PDF must not hide the rest; failures are reported, not skipped silently
        return {"doc_id": entry["file"], "error": f"{type(exc).__name__}: {exc}"}


def _extract(entry: dict, max_pages: int, t0: float) -> dict:
    doc = pdf_ingest.pdf_to_doc(
        HERE / "data" / entry["file"],
        f"{entry['publisher']}:{entry['file']}",
        [GROUP[entry["publisher"]]],
        title=entry["file"],
        max_pages=max_pages,
    )
    doc["publisher"] = entry["publisher"]
    doc["stats"]["seconds"] = round(time.time() - t0, 1)
    return doc


if __name__ == "__main__":
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    manifest = json.loads((HERE / "MANIFEST.json").read_text())
    with Pool(12) as pool:
        docs = []
        for d in pool.imap_unordered(work, [(e, max_pages) for e in manifest]):
            docs.append(d)
            print(len(docs), d["doc_id"][:50], d["stats"], flush=True)
    failed = [d for d in docs if "error" in d]
    docs = sorted((d for d in docs if "error" not in d), key=lambda d: d["doc_id"])
    (HERE / "data" / "failed.json").write_text(json.dumps(failed))
    print("FAILED", failed)
    (HERE / "data" / "corpus.json").write_text(json.dumps(docs))
