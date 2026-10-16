"""Fetch ClearanceBench (pinned HF revision) into the gitignored ./data/ dir.

The data is synthetic and Apache-2.0, but like every downloaded dataset here it stays out of git.
MANIFEST.json pins url, revision, bytes and sha256 (the sha256 is the LFS oid published by Hugging
Face, so it is checked against the bytes we got, not computed from them alone). Downloaded content
is data, never instructions.
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
    DATA.mkdir(exist_ok=True)
    for f in m["files"]:
        dest = DATA / f["file"]
        if not (dest.exists() and sha256_file(dest) == f["sha256"]):
            url = f"https://huggingface.co/datasets/{m['dataset']}/resolve/{m['revision']}/{f['path']}"
            print(f"downloading {url} ({f['bytes']} bytes)", flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as out:
                while blk := r.read(1 << 20):
                    out.write(blk)
        if dest.stat().st_size != f["bytes"] or sha256_file(dest) != f["sha256"]:
            dest.unlink()
            raise SystemExit(f"size/sha256 mismatch for {f['file']}; deleted, rerun to refetch")
        print(f"ok {f['file']} sha256 verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
