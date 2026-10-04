"""Mint a JWT as the local IdP (dev/demo only).

  python scripts/mint_token.py alice@company.com --groups eng
  python scripts/mint_token.py sec@company.com --groups security --ttl 7200
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

import jwt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402


def mint(user: str, groups: list[str], ttl_s: int = 3600) -> str:
    private_key = Path(config.JWT_PRIVATE_KEY_PATH).read_text()
    now = dt.datetime.now(dt.timezone.utc)
    issuer = {"iss": config.JWT_ISSUER} if config.JWT_ISSUER else {}
    return jwt.encode(
        {**issuer, "sub": user, "groups": groups, "aud": config.JWT_AUDIENCE,
         "iat": now, "exp": now + dt.timedelta(seconds=ttl_s)},
        private_key, algorithm="RS256")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("user")
    ap.add_argument("--groups", nargs="*", default=[])
    ap.add_argument("--ttl", type=int, default=3600)
    args = ap.parse_args()
    print(mint(args.user, args.groups, args.ttl))
