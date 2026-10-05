"""The demo that markets itself: same query, three users, three answers.

  python scripts/demo.py                       # needs the API on :8090
  python scripts/demo.py "what are the salary bands?"
"""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.mint_token import mint  # noqa: E402

API = "http://localhost:8090"
USERS = [
    ("alice@company.com", ["eng"]),
    ("bob@company.com", ["hr"]),
    ("guest@external.com", []),
]

if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "what are the salary bands?"
    print(f'QUERY: "{query}"\n')
    for user, groups in USERS:
        token = mint(user, groups)
        r = httpx.post(f"{API}/query", json={"query": query},
                       headers={"Authorization": f"Bearer {token}"}, timeout=30)
        body = r.json()
        print(f"— {user} (groups={groups or 'none'})")
        print(f"  answer: {body.get('answer')}")
        print(f"  docs:   {[x['doc_id'] for x in body.get('results', [])] or '[]'}\n")

    sec = mint("sec@company.com", ["security"])
    audit = httpx.get(f"{API}/audit", headers={"Authorization": f"Bearer {sec}"}).json()
    print("— audit (as security):")
    for row in audit["recent"][:3]:
        print(f"  {row['user']}: \"{row['query']}\" -> {row['returned_docs']} "
              f"(denied {row['denied_count']} chunks)")
