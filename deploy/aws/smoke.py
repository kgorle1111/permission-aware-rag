"""Smoke a local or deployed synthetic demo without an LLM call or credentials."""

import json
import sys
import time
import urllib.error
import urllib.request


def smoke(base):
    base = base.rstrip("/")
    for attempt in range(30):
        try:
            with urllib.request.urlopen(base + "/", timeout=5) as response:
                if response.status == 200 and b"<html" in response.read().lower():
                    break
        except (OSError, urllib.error.URLError):
            if attempt == 29:
                raise
        time.sleep(1)
    else:
        raise RuntimeError("demo UI did not become healthy")

    def post(path, body):
        request = urllib.request.Request(
            base + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    junior = post("/query", {"user": "junior", "q": "claims history policy 10023"})
    if not any(r["doc_id"] == "claims-10023" for r in junior["results"]):
        raise RuntimeError("expected junior retrieval missing")
    forbidden = post("/query", {"user": "junior", "q": "Bank profile Delgado account balance"})
    allowed = {"policy-10023", "policy-10088", "claims-10023", "guidelines"}
    if any(r["doc_id"] not in allowed for r in forbidden["results"]):
        raise RuntimeError("junior received a forbidden document")
    answer = post("/ask", {"user": "junior", "q": "policy 10023 status"})
    if "answer" in answer or not answer.get("note", "").startswith("Set ANTHROPIC_API_KEY"):
        raise RuntimeError("public demo must be retrieval-only")
    with urllib.request.urlopen(base + "/audit?user=auditor", timeout=10) as response:
        entries = json.load(response)["entries"]
    if not entries or any(e["query"] != "[redacted]" for e in entries if e["user"] != "auditor"):
        raise RuntimeError("audit query redaction failed")
    print("Demo smoke passed: UI, allowed retrieval, forbidden-document check, no LLM, audit redaction.")


if __name__ == "__main__":
    smoke(sys.argv[1])
