"""OCR accuracy on real PDF pages.

born  : rasterize born-digital pages (200 and 300 DPI), OCR with the production
        pdf_ingest.ocr_page, score against the page's OWN text layer (exact ground
        truth for the characters; reading order is the column-aware extractor's).
fr    : OCR Federal Register scan pages and compare with GPO's embedded OCR layer.
        That layer is uncorrected machine OCR: agreement is a NOISY baseline, NOT
        accuracy.

usage: python ocr_accuracy.py {born|fr} OUT.json   (run from anywhere; needs tesseract)
"""

from __future__ import annotations

import json
import os
import random
import sys
import unicodedata
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # one tesseract thread per worker process
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "platform"))

import pdfplumber  # noqa: E402
from stats import bag_recall, edit_distance  # noqa: E402

from app import pdf_ingest  # noqa: E402

QUOTA = {"FDIC": 40, "IRS": 40, "GAO": 20, "HUD": 15, "CFPB": 15, "Treasury": 15, "FEMA": 3}
DPIS = (200, 300)
_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-"})


def norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).translate(_MAP).split())


def truth_text(page, mode: str) -> str:
    if mode == "born":
        return norm(" ".join(x["text"] for x in pdf_ingest._page_lines(page)))
    return norm(page.extract_text() or "")


def score(truth: str, ocr: str) -> dict:
    tw, ow = truth.split(), ocr.split()
    hit, total = bag_recall(tw, ow)
    return {
        "chars": len(truth),
        "char_edits": edit_distance(truth, ocr),
        "words": len(tw),
        "word_edits": edit_distance(tw, ow),
        "bag_hit": hit,
        "bag_total": total,
    }


def work(task: dict) -> dict:
    with pdfplumber.open(task["path"]) as pdf:
        page = pdf.pages[task["page"]]
        truth = truth_text(page, task["mode"])
        out = []
        for dpi in task["dpis"]:
            out.append({"dpi": dpi, **score(truth, norm(pdf_ingest.ocr_page(page, dpi)))})
    return {**{k: task[k] for k in ("publisher", "file", "page")}, "runs": out}


def sample(mode: str, rng: random.Random) -> list[dict]:
    manifest = json.loads((HERE / "MANIFEST.json").read_text())
    kind = "born_digital" if mode == "born" else "scanned"
    by_pub: dict[str, list[dict]] = defaultdict(list)
    for e in manifest:
        if e["kind"] == kind:
            by_pub[e["publisher"]].append(e)
    tasks = []
    for pub, entries in sorted(by_pub.items()):
        quota = QUOTA.get(pub, 0) if mode == "born" else 32
        pages = []
        for e in entries:
            with pdfplumber.open(HERE / "data" / e["file"]) as pdf:
                pages += [(e, i) for i in range(len(pdf.pages))]
        rng.shuffle(pages)
        got, seen_docs = 0, defaultdict(int)
        for e, i in pages:
            if got >= quota:
                break
            if mode == "fr" and seen_docs[e["file"]] >= 2:
                continue
            path = HERE / "data" / e["file"]
            with pdfplumber.open(path) as pdf:
                n = len(truth_text(pdf.pages[i], mode))
            if n < (300 if mode == "born" else 800):
                continue
            tasks.append(
                {
                    "publisher": pub,
                    "file": e["file"],
                    "path": str(path),
                    "page": i,
                    "mode": mode,
                    "dpis": DPIS if mode == "born" else (300,),
                }
            )
            seen_docs[e["file"]] += 1
            got += 1
    return tasks


if __name__ == "__main__":
    mode, out = sys.argv[1], Path(sys.argv[2])
    tasks = sample(mode, random.Random(20261010))
    print(f"{len(tasks)} pages", flush=True)
    with Pool(8) as pool:
        results = []
        for r in pool.imap(work, tasks):
            results.append(r)
            print(len(results), r["publisher"], r["file"][-24:], r["page"], flush=True)
    out.write_text(json.dumps({"mode": mode, "pages": results}, indent=1))
