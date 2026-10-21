"""Consistent SQLite copy via the sqlite3 backup API, then integrity-check the copy.

Stdlib only: backup.sh pipes this file into `python -` inside the image, so it
must run without the app's dependencies or the host having sqlite3 installed.

Usage: python sqlite_backup.py SRC_DB DST_DB
"""
import sqlite3
import sys
from pathlib import Path


def backup(src: str | Path, dst: str | Path) -> None:
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        # sqlite3.connect would silently create an empty database here, and an
        # empty rag.db restores as "no ACLs, no audit": refuse instead.
        raise FileNotFoundError(f"source database {src} does not exist")
    if dst.exists():
        raise FileExistsError(f"refusing to overwrite {dst}")
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
    if result != "ok":
        dst.unlink()
        raise RuntimeError(f"backup copy failed integrity_check: {result}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    backup(argv[0], argv[1])
    print(f"sqlite backup ok: {argv[0]} -> {argv[1]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
