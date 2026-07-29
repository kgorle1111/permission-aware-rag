"""Endpoint tests against a real in-process server on an ephemeral port.

Run: pytest test_http.py. No API key needed — /ask exercises the no-LLM path.
"""

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import underwriter_server as srv

srv.rag.audit_path = None  # tests must not write the real audit log


def _get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _post(base, path, body, headers=None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data, {"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _jwt(secret, claims):
    def enc(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    h, p = enc({"alg": "HS256", "typ": "JWT"}), enc(claims)
    sig = (
        base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    return f"{h}.{p}.{sig}"


def test():
    server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # 400s: missing params, unknown user, oversized q
        assert _get(base, "/query?q=policy")[0] == 400
        assert _get(base, "/query?user=nobody&q=policy")[0] == 400
        assert _get(base, "/ask?user=junior&q=" + "a" * 1100)[0] == 400

        # /query shape
        code, d = _get(base, "/query?user=junior&q=claims+history+policy+10023")
        assert code == 200 and {"results", "denied_chunks"} <= set(d)
        assert d["results"][0]["doc_id"] == "claims-10023"

        # /ask without a key: retrieval + note, no answer
        code, d = _get(base, "/ask?user=senior&q=delgado+credit+memo")
        assert code == 200 and "note" in d and "answer" not in d

        # /ask with zero results: skips LLM with the explicit note
        code, d = _get(base, "/ask?user=junior&q=zzqqxx")
        assert d["note"].startswith("No accessible documents matched")

        # /audit scoping: junior sees only own entries; auditor sees everyone
        code, d = _get(base, "/audit?user=junior")
        assert code == 200 and all(e["user"] == "junior" for e in d["entries"])
        code, d = _get(base, "/audit?user=auditor")
        assert {e["user"] for e in d["entries"]} >= {"junior", "senior"}

        # POST works for /query and /ask; malformed body is a clean 400
        code, d = _post(base, "/query", {"user": "junior", "q": "claims history policy 10023"})
        assert code == 200 and d["results"][0]["doc_id"] == "claims-10023"
        assert _post(base, "/ask", {"user": "junior", "q": "zzqqxx"})[1]["note"].startswith("No accessible")
        assert _post(base, "/query", b"not json")[0] == 400

        # /presets serves the JSON file
        code, d = _get(base, "/presets")
        assert code == 200 and isinstance(d, list) and len(d[0]) == 2

        # SHOW_DENIED off: denied count absent from responses
        srv.SHOW_DENIED = False
        try:
            assert "denied_chunks" not in _get(base, "/query?user=junior&q=policy")[1]
        finally:
            srv.SHOW_DENIED = True

        # JWT seam: valid token resolves claims; bad/missing token is 401
        srv.JWT_SECRET = "test-secret"
        try:
            tok = _jwt(
                "test-secret", {"sub": "sso-user", "groups": ["underwriting"], "exp": time.time() + 60}
            )
            code, d = _get(base, "/query?q=claims+history", {"Authorization": f"Bearer {tok}"})
            assert code == 200 and d["results"]
            assert _get(base, "/query?q=x", {"Authorization": "Bearer bad.token.sig"})[0] == 401
            assert _get(base, "/query?user=junior&q=x")[0] == 401  # dropdown ignored in JWT mode
            expired = _jwt("test-secret", {"sub": "u", "groups": [], "exp": time.time() - 5})
            assert _get(base, "/query?q=x", {"Authorization": f"Bearer {expired}"})[0] == 401
        finally:
            srv.JWT_SECRET = None

        # cost observability: mocked LLM answer carries llm_ms + est_cost; /audit totals accrue
        from unittest import mock

        usage = {"input_tokens": 1000, "output_tokens": 300, "cache_read_input_tokens": 2000}
        with mock.patch.object(
            srv.llm,
            "ask",
            return_value={"answer": "ok [policy-10023]", "usage": usage, "unverified_citations": []},
        ):
            code, d = _post(base, "/ask", {"user": "junior", "q": "policy 10023 status"})
        assert code == 200 and "llm_ms" in d
        assert d["est_cost_usd"] == round((1000 * 1.00 + 300 * 5.00 + 2000 * 0.10) / 1e6, 6)
        code, d = _get(base, "/audit?user=junior")
        assert d["llm_summary"]["asks"] >= 1 and d["llm_summary"]["est_cost_usd"] > 0

        # /audit CSV export: header row + scoped to caller
        with urllib.request.urlopen(base + "/audit?user=junior&format=csv") as r:
            assert r.headers["Content-Type"] == "text/csv"
            lines = r.read().decode().splitlines()
        assert lines[0].startswith("ts,user,query") and all(",junior," in ln for ln in lines[1:])

        # rate limit: /ask 429s within the window (runs last — it exhausts the bucket)
        codes = [_get(base, f"/ask?user=junior&q=policy+{i}")[0] for i in range(srv.RATE_LIMIT + 2)]
        assert 429 in codes and codes[-1] == 429
    finally:
        server.shutdown()
    print("all http tests passed")


if __name__ == "__main__":
    test()
