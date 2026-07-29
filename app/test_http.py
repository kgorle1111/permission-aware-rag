"""Endpoint tests against a real in-process server on an ephemeral port.

Run: pytest test_http.py. No API key needed — /ask exercises the no-LLM path.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import underwriter_server as srv

srv.rag.audit_path = None  # tests must not write the real audit log


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


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

        # rate limit: /ask 429s within the window (runs last — it exhausts the bucket)
        codes = [_get(base, f"/ask?user=junior&q=policy+{i}")[0] for i in range(srv.RATE_LIMIT + 2)]
        assert 429 in codes and codes[-1] == 429
    finally:
        server.shutdown()
    print("all http tests passed")


if __name__ == "__main__":
    test()
