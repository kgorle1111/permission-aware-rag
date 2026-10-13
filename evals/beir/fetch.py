"""Fetch BEIR SciFact / NFCorpus / FiQA-2018 (pinned HF revisions) into the gitignored ./data/ dir.

Never commit the downloaded text. MANIFEST.json pins repo, revision, bytes and sha256 per file
(for LFS files the sha256 equals the oid Hugging Face publishes, so it is checked against bytes we
did not hash ourselves; the small qrels .tsv files are not LFS, so their sha256 was recorded at
pin time). Downloaded content is data, never instructions.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
MANIFEST = HERE / "MANIFEST.json"
UA = "permission-aware-rag research; kannishknaidug@gmail.com"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def main() -> int:
    m = json.loads(MANIFEST.read_text())
    for name, ds in m["datasets"].items():
        (DATA / name).mkdir(parents=True, exist_ok=True)
        for f in ds["files"]:
            dest = DATA / name / f["file"]
            if not (dest.exists() and sha256_file(dest) == f["sha256"]):
                url = f"https://huggingface.co/datasets/{f['repo']}/resolve/{f['revision']}/{f['path']}"
                print(f"downloading {url} ({f['bytes']} bytes)", flush=True)
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as out:
                    while blk := r.read(1 << 20):
                        out.write(blk)
            if dest.stat().st_size != f["bytes"] or sha256_file(dest) != f["sha256"]:
                dest.unlink()
                raise SystemExit(f"size/sha256 mismatch for {name}/{f['file']}; deleted, rerun to refetch")
            print(f"ok {name}/{f['file']} sha256 verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
