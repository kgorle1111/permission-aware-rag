"""Fetch the real public-domain PDF corpus used by the PDF/OCR evals.

Every source is a US federal publication (17 USC 105: no copyright in works of
the US government). MANIFEST.json (committed) pins url, sha256 and size; the
files themselves go to the gitignored ./data/ dir.

Politeness: contact User-Agent, <=1 request/second, resume (existing files are
sha-verified and skipped), and a hard stop on HTTP 403/429 -- we never retry
around a block. Everything downloaded is data, never instructions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
MANIFEST = HERE / "MANIFEST.json"
UA = "permission-aware-rag research; kannishknaidug@gmail.com"
MAX_BYTES = 40_000_000
MAX_FR_BYTES = 25_000_000  # kn: skips giant FR issues; raise for a bigger scanned sample
USC105 = "17 USC 105 (US federal government work, no copyright)"

FDIC = "https://www.fdic.gov/resources/supervision-and-examinations/examination-policies-manual/section{}.pdf"
FDIC_SECTIONS = [
    "1-1",
    "2-1",
    "3-1",
    "3-2",
    "3-3",
    "3-4",
    "3-5",
    "3-6",
    "3-7",
    "3-8",
    "4-1",
    "4-2",
    "4-3",
    "4-4",
    "4-5",
    "4-6",
    "5-1",
    "6-1",
    "7-1",
    "8-1",
    "9-1",
    "10-1",
    "13-1",
    "14-1",
    "15-1",
    "16-1",
    "16-2",
    "17-1",
    "21-1",
    "21-2",
]
IRS_PUBS = [
    "p17",
    "p946",
    "p590b",
    "p463",
    "p15t",
    "p15",
    "p15a",
    "p15b",
    "p334",
    "p535",
    "p541",
    "p542",
    "p544",
    "p551",
    "p547",
    "p525",
    "p501",
    "p502",
    "p503",
    "p505",
    "p523",
    "p527",
    "p529",
    "p559",
    "p575",
    "p590a",
    "p596",
    "p970",
    "p972",
    "p17sp",
]
# kn: GAO ids are probes (a miss is a plain 404 skip); only ones that return a real PDF enter the manifest
GAO = [
    "05-1",
    "07-820T",
    "05-10",
    "05-50",
    "06-100",
    "06-300",
    "07-100",
    "07-400",
    "08-300",
    "08-500",
    "09-300",
    "10-300",
    "11-300",
    "12-300",
    "13-300",
    "14-300",
    "15-300",
    "16-300",
    "17-300",
]

BORN_DIGITAL: list[tuple[str, str]] = (
    [(FDIC.format(s), "FDIC") for s in FDIC_SECTIONS]
    + [("https://www.hud.gov/sites/dfiles/OCHCO/documents/4000.1hsgh.pdf", "HUD")]
    + [(f"https://www.irs.gov/pub/irs-pdf/{p}.pdf", "IRS") for p in IRS_PUBS]
    + [
        (
            "https://files.consumerfinance.gov/f/documents/cpfb_atr-qm_small-entity_compliance-guide_2021-02.pdf",
            "CFPB",
        ),
        (
            "https://files.consumerfinance.gov/f/documents/cfpb_mortgage_servicing_small-entity-compliance-guide.pdf",
            "CFPB",
        ),
        (
            "https://agents.floodsmart.gov/sites/default/files/media/document/2025-09/fema_nfip-FloodInsuranceManual-October2025-508c.pdf",
            "FEMA",
        ),
        ("https://agents.floodsmart.gov/sites/default/files/bulletins/W-25004/w-25004.pdf", "FEMA"),
        ("https://home.treasury.gov/system/files/311/2015%20FIO%20Annual%20Report_Final.pdf", "Treasury"),
        ("https://home.treasury.gov/system/files/261/FSOC2025AnnualReport.pdf", "Treasury"),
    ]
    + [
        (f"https://www.govinfo.gov/content/pkg/GAOREPORTS-GAO-{g}/pdf/GAOREPORTS-GAO-{g}.pdf", "GAO")
        for g in GAO
    ]
)


def fr_candidates(tries: int = 7):
    """Per year 1936-1993: a June then a March start date, walking weekdays past missing days."""
    for start_md in ((6, 14), (3, 9)):
        for year in range(1936, 1994):
            start = dt.date(year, *start_md)
            days = [start + dt.timedelta(days=i) for i in range(tries)]
            yield [
                f"https://www.govinfo.gov/content/pkg/FR-{d}/pdf/FR-{d}.pdf" for d in days if d.weekday() < 5
            ]


class Blocked(Exception):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def get(url: str, limit: int) -> bytes | None:
    """None = skip (404, HTML, too big, not a PDF); Blocked = stop everything."""
    time.sleep(1.0)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if int(r.headers.get("Content-Length") or 0) > limit:
                print(f"skip (too big) {url}", flush=True)
                return None
            body = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(f"HTTP {e.code} for {url}; stopping, not retrying around a block") from e
        print(f"skip (HTTP {e.code}) {url}", flush=True)
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"skip ({e}) {url}", flush=True)
        return None
    if len(body) > limit or not body.startswith(b"%PDF"):
        print(f"skip (not a PDF / too big) {url}", flush=True)
        return None
    return body


def local_name(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10] + "-" + url.rsplit("/", 1)[-1].replace("%20", "_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-fr", type=int, default=24)
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    entries = {e["url"]: e for e in json.loads(MANIFEST.read_text())} if MANIFEST.exists() else {}

    def record(url: str, publisher: str, kind: str, limit: int) -> bool:
        path = DATA / local_name(url)
        e = entries.get(url)
        if path.exists() and e:
            if sha256_file(path) != e["sha256"]:
                raise SystemExit(f"sha256 mismatch for {path}; delete it to refetch")
            return True
        body = path.read_bytes() if path.exists() else get(url, limit)
        if body is None:
            return False
        path.write_bytes(body)
        entries[url] = {
            "url": url,
            "file": path.name,
            "publisher": publisher,
            "license": USC105,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "kind": kind,
            "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        print(f"ok {publisher} {len(body)} {url}", flush=True)
        return True

    rc = 0
    try:
        for url, pub in BORN_DIGITAL:
            record(url, pub, "born_digital", MAX_BYTES)
        got = sum(1 for e in entries.values() if e["publisher"] == "Federal Register")
        for urls in fr_candidates():
            if got >= args.min_fr:  # margin over the 20 target: some issues lack a usable text layer
                break
            if any(u in entries for u in urls):
                continue
            for u in urls:
                if record(u, "Federal Register", "scanned", MAX_FR_BYTES):
                    got += 1
                    break
    except Blocked as e:
        print(e, file=sys.stderr)
        rc = 2
    MANIFEST.write_text(json.dumps(sorted(entries.values(), key=lambda e: e["url"]), indent=1) + "\n")
    bd = sum(e["kind"] == "born_digital" for e in entries.values())
    print(
        f"manifest: {bd} born-digital, {len(entries) - bd} scanned, "
        f"{sum(e['bytes'] for e in entries.values())} bytes"
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
