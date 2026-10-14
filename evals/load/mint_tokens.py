"""Mint one long-lived demo token per persona (needs the platform venv: PyJWT).

Local test only: signs with the keypair from platform/scripts/gen_keys.py. Never use against a deployment.

  python evals/load/mint_tokens.py tokens.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform"))
from lib import PERSONAS  # noqa: E402
from scripts.mint_token import mint  # noqa: E402

if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps({u: mint(u, g, ttl_s=6 * 3600) for u, g in PERSONAS}))
