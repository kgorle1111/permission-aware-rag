"""Pure helpers for the load test: percentile, Wilson bound, personas and the permission oracle.

The oracle is deliberately independent of platform code: it re-derives who may read each chunk from
the corpus files (document AND section AND paragraph ACLs, plus the permissions.jsonl patch feed).
Stdlib only.
"""

import json
import math
from pathlib import Path

# (user, groups): mixed roles so some queries are denied, some answered.
PERSONAS = [
    ("alice@company.com", ["eng"]),
    ("bob@company.com", ["hr"]),
    ("ceo@company.com", ["board", "exec"]),
    ("guest@external.com", []),
    ("sec@company.com", ["security"]),
    ("fin@company.com", ["finance"]),
    ("dana@company.com", ["eng", "finance"]),
    ("eve@company.com", ["hr", "finance"]),
    ("frank@company.com", []),
    ("gina@company.com", ["eng", "hr"]),
]

QUERIES = [
    "vacation policy and paid days off",
    "on-call rotation pager handoff",
    "salary bands compensation",
    "executive compensation CEO package",
    "board minutes acquisition",
    "company strategy mid-market customers",
    "financial projections revenue target",
    "remote work policy",
    "expense policy meals reimbursed",
    "what are the salary bands?",
]


def principals(user: str, groups: list[str]) -> set[str]:
    return {f"user:{user}", *(f"group:{g}" for g in groups), "*"}


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank (same definition as platform/app/observability.percentile)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1]


def wilson_upper(failures: int, n: int, z: float = 1.96) -> float:
    """Upper limit of the Wilson score interval for a failure rate (z=1.96 is two-sided 95%)."""
    if n <= 0:
        return 1.0
    p = failures / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + margin) / (1 + z * z / n))


class Oracle:
    """text -> allowed ACL levels, per doc. A chunk is readable iff the caller holds a grant at every level."""

    def __init__(self, docs: list[dict], permissions: list[dict]):
        patch = {p["doc_id"]: p for p in permissions}
        self.chunks: dict[str, list[tuple[str, list[list[str]]]]] = {}
        for doc in docs:
            p = patch.get(doc["doc_id"], {})
            doc_acl = [] if p.get("deleted") else p.get("acl", doc.get("acl", []))
            for sec in doc["sections"]:
                sec_acl = sec.get("acl", ["*"])
                paras = sec.get("paragraphs") or [{"text": t} for t in sec["text"].split("\n\n")]
                for para in paras:
                    text = para["text"].strip()
                    if text:
                        levels = [doc_acl, sec_acl, para.get("acl", ["*"])]
                        self.chunks.setdefault(doc["doc_id"], []).append((text, levels))

    @classmethod
    def from_files(cls, docs_path: Path, perms_path: Path) -> "Oracle":
        perms = [json.loads(line) for line in perms_path.read_text().splitlines() if line.strip()]
        return cls(json.loads(docs_path.read_text()), perms)

    def violations(self, results: list[dict], held: set[str]) -> list[str]:
        """Returned results the caller may not read. Text that matches no chunk of the doc counts
        as a violation: unknown content must not be assumed safe."""
        bad = []
        for r in results:
            ok = False
            for text, levels in self.chunks.get(r.get("doc_id"), []):
                if r.get("text", "")[:40] == text[:40] and all(held & set(level) for level in levels):
                    ok = True
                    break
            if not ok:
                bad.append(r.get("doc_id", "?"))
        return bad
