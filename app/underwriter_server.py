"""Underwriter workbench: permission-aware retrieval + optional LLM answers.

Run: python3 underwriter_server.py [port]
Set ANTHROPIC_API_KEY to enable /ask (LLM answers with prompt caching);
without it, /ask returns retrieval-only results with a note.
"""
import json
import pathlib
import sys
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/query", "/ask"):
            qs = parse_qs(url.query)
            user = USERS.get(qs.get("user", [""])[0])
            query = qs.get("q", [""])[0]
            if not user or not query:
                self._json(400, {"error": "need user and q"})
                return
            results = rag.retrieve(query, user, k=4)
            out = {"results": results, "denied_chunks": rag.audit[-1]["denied_chunks"]}
            if url.path == "/ask":
                try:
                    llm_out = llm.ask(query, results)
                except Exception as e:  # LLM failure must not take down retrieval
                    llm_out = None
                    out["note"] = f"LLM call failed ({e}); showing retrieval only."
                if llm_out:
                    out["answer"] = llm_out["answer"]
                    out["usage"] = llm_out["usage"]
                elif "note" not in out:
                    out["note"] = "Set ANTHROPIC_API_KEY to enable drafted answers; showing retrieval only."
            self._json(200, out)
        elif url.path == "/audit":
            user = USERS.get(parse_qs(url.query).get("user", [""])[0])
            if not user:
                self._json(400, {"error": "need user"})
                return
            self._json(200, {"entries": audit_for(user)})
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(UI.read_bytes())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8421
    print(f"http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
