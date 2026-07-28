"""Run permission sync: one pass, or watch mode (polling reconciliation).

  python scripts/sync_run.py             # one pass
  python scripts/sync_run.py --watch 10  # poll every 10s (= staleness bound)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import init_db  # noqa: E402
from app.sync import sync_once, watch  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=float, metavar="SECONDS", default=0)
    args = ap.parse_args()
    init_db()
    if args.watch:
        watch(args.watch)
    else:
        changed = sync_once()
        print(f"sync applied. changed docs: {changed or 'none'}")
