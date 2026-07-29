"""Underwriter workbench: permission-aware retrieval + optional LLM answers.

Run: python3 underwriter_server.py [port]
Set ANTHROPIC_API_KEY to enable /ask (LLM answers with prompt caching);
without it, /ask returns retrieval-only results with a note.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import pathlib
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import llm
from permission_rag import PermissionRAG

# Roles: junior underwriters see policy/claims; banking group sees financials;
# compliance sees the watchlist; senior sees credit memos on top of everything.
USERS = {
    "junior": {"id": "junior", "groups": ["underwriting"]},
    "senior": {"id": "senior", "groups": ["underwriting", "banking", "senior"]},
    "compliance": {"id": "compliance", "groups": ["underwriting", "compliance"]},
    "auditor": {"id": "auditor", "groups": ["compliance", "banking", "audit"]},
}

rag = PermissionRAG(audit_path=pathlib.Path(__file__).with_name("audit_log.jsonl"))
rag.add_document("policy-10023", "Policy 10023 status: ACTIVE. Homeowners, insured Maria Chen, coverage 450000 dollars, premium paid through December 2026. Prior carrier lapse of 30 days in 2023.", {"group:underwriting"})
rag.add_document("policy-10088", "Policy 10088 status: PENDING RENEWAL. Commercial property, insured Delgado Logistics LLC, coverage 2.1 million dollars. Renewal blocked pending updated roof inspection report.", {"group:underwriting"})
rag.add_document("claims-10023", "Claims history for policy 10023: one water damage claim in March 2024, paid 12400 dollars, subrogation recovered 8000 dollars. No open claims.", {"group:underwriting"})
rag.add_document("bank-delgado", "Bank profile Delgado Logistics LLC: operating account average balance 310000 dollars, two NSF events in the last twelve months, line of credit 500000 dollars at 72 percent utilization.", {"group:banking"})
rag.add_document("credit-memo-delgado", "Credit memo: Delgado Logistics debt service coverage ratio 1.1, below the 1.25 threshold. Recommend additional collateral or premium loading before binding above 1 million.", {"group:senior"})
rag.add_document("watchlist", "Compliance watchlist: Delgado Logistics principal Robert Delgado is under review for a 2025 misrepresentation flag on a prior marine cargo application. Do not bind without compliance sign-off.", {"group:compliance"})
rag.add_document("guidelines", "Underwriting guideline excerpt: properties with a prior coverage lapse over 21 days require senior review. Commercial risks above 2 million require a current inspection dated within 12 months.", {"*"})

UI = pathlib.Path(__file__).with_name("ui.html")
PRESETS = pathlib.Path(__file__).with_name("presets.json")

MAX_Q_LEN = 1000
MAX_BODY = 16 * 1024

# The "N hidden" count is a deliberate demo side channel (it sells the feature).
# Default off outside the demo: SHOW_DENIED=0 removes it from responses.
SHOW_DENIED = os.environ.get("SHOW_DENIED", "1") == "1"

# SSO seam: set UNDERWRITER_JWT_SECRET and identity comes from a signed HS256
# JWT (sub + groups claims) instead of the demo dropdown. can_read() unchanged.
JWT_SECRET = os.environ.get("UNDERWRITER_JWT_SECRET")


def _b64url(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def user_from_jwt(auth_header):
    """Validate 'Bearer <hs256 jwt>' -> {"id", "groups"} or None."""
    try:
        h, p, sig = auth_header.split()[1].split(".")
        expected = hmac.new(JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(sig), expected):
            return None
        claims = json.loads(_b64url(p))
        if claims.get("exp", 0) < time.time():
            return None
        return {"id": claims["sub"], "groups": list(claims.get("groups", []))}
    except Exception:
        return None


def resolve_user(handler, qs, body):
    """JWT when configured; demo dropdown (?user= / body user) otherwise."""
    if JWT_SECRET:
        return user_from_jwt(handler.headers.get("Authorization", ""))
    uid = (body or {}).get("user") or qs.get("user", [""])[0]
    return USERS.get(uid)

# /ask is a paid API call; bound it per client IP before this ever leaves loopback.
RATE_LIMIT, RATE_WINDOW = 10, 60.0  # ponytail: in-memory per-process; shared store if this ever scales out
_hits = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
_rate_lock = threading.Lock()


# Haiku 4.5 $/MTok (2026-07): input 1.00, output 5.00, cache read 0.10, cache write 1.25
PRICE = {"input_tokens": 1.00, "output_tokens": 5.00,
         "cache_read_input_tokens": 0.10, "cache_creation_input_tokens": 1.25}

# Running value-receipt totals for /audit — per-process, resets on restart.
TOTALS = {"asks": 0, "input_tokens": 0, "output_tokens": 0,
          "cache_read_input_tokens": 0, "est_cost_usd": 0.0, "llm_ms": 0.0}
_totals_lock = threading.Lock()


def est_cost(usage):
    return round(sum(usage.get(k, 0) * p for k, p in PRICE.items()) / 1e6, 6)


def record_ask(usage, llm_ms):
    with _totals_lock:
        TOTALS["asks"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            TOTALS[k] += usage.get(k, 0)
        TOTALS["est_cost_usd"] = round(TOTALS["est_cost_usd"] + est_cost(usage), 6)
        TOTALS["llm_ms"] = round(TOTALS["llm_ms"] + llm_ms, 1)


def rate_limited(ip):
    now = time.monotonic()
    with _rate_lock:
        dq = _hits[ip]
        if len(dq) == RATE_LIMIT and now - dq[0] < RATE_WINDOW:
            return True
        dq.append(now)
    return False


def audit_for(user):
    """Audit entries visible to this user: own entries only, unless in the audit group."""
    entries = rag.audit if "audit" in user["groups"] else \
        [e for e in rag.audit if e["user"] == user["id"]]
    return entries[-50:]



class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _qa(self, path, user, query):
        """Shared /query and /ask logic; caller has already resolved identity."""
        if not user:
            self._json(401 if JWT_SECRET else 400,
                       {"error": "valid bearer token required" if JWT_SECRET else "need user and q"})
            return
        if not query:
            self._json(400, {"error": "need user and q"})
            return
        if len(query) > MAX_Q_LEN:
            self._json(400, {"error": f"q too long (max {MAX_Q_LEN} chars)"})
            return
        if path == "/ask" and rate_limited(self.client_address[0]):
            self._json(429, {"error": "rate limited; retry in a minute"})
            return
        results = rag.retrieve(query, user, k=4)
        out = {"results": results}
        if SHOW_DENIED:
            out["denied_chunks"] = rag.audit[-1]["denied_chunks"]
        if path == "/ask":
            llm_out = None
            if not results:
                out["note"] = "No accessible documents matched; skipped the LLM call."
            else:
                t0 = time.perf_counter()
                try:
                    llm_out = llm.ask(query, results)
                except Exception as e:  # LLM failure must not take down retrieval
                    print(f"llm error: {e}", file=sys.stderr)  # detail stays server-side
                    out["note"] = "LLM call failed; showing retrieval only."
            if llm_out:
                llm_ms = round((time.perf_counter() - t0) * 1000, 1)
                record_ask(llm_out["usage"], llm_ms)
                out["answer"] = llm_out["answer"]
                out["usage"] = llm_out["usage"]
                out["llm_ms"] = llm_ms
                out["est_cost_usd"] = est_cost(llm_out["usage"])
                out["unverified_citations"] = llm_out["unverified_citations"]
            elif "note" not in out:
                out["note"] = "Set ANTHROPIC_API_KEY to enable drafted answers; showing retrieval only."
        self._json(200, out)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path not in ("/query", "/ask"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            self._json(400, {"error": "need a JSON body"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            self._json(400, {"error": "malformed JSON body"})
            return
        self._qa(url.path, resolve_user(self, {}, body), str(body.get("q", "")))

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/query", "/ask"):
            qs = parse_qs(url.query)
            self._qa(url.path, resolve_user(self, qs, None), qs.get("q", [""])[0])
        elif url.path == "/presets":
            self._json(200, json.loads(PRESETS.read_text()))
        elif url.path == "/audit":
            qs = parse_qs(url.query)
            user = resolve_user(self, qs, None)
            if not user:
                self._json(400, {"error": "need user"})
                return
            entries = audit_for(user)
            if qs.get("format", [""])[0] == "csv":  # compliance needs the trail out of the browser
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["ts", "user", "query", "returned", "denied_chunks", "elapsed_ms"])
                for e in entries:
                    w.writerow([e["ts"], e["user"], e["query"], ";".join(e["returned"]),
                                e["denied_chunks"], e.get("elapsed_ms", "")])
                body = buf.getvalue().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=audit.csv")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            with _totals_lock:
                summary = dict(TOTALS)
            self._json(200, {"entries": entries, "llm_summary": summary})
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("X-Content-Type-Options", "nosniff")
            # inline script/style are how the single-file UI ships; CSP still blocks all external loads
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(UI.read_bytes())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8421
    print(f"http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
