"""Red-team suite against the real HTTP surface, with a MOCKED model.

What a pass proves: structural properties. The permission filter runs before the model, so
restricted text never reaches the prompt; retrieved text is escaped inside <document> blocks;
the system prompt is untouched; identity comes only from the server-side token; malformed and
oversized input is rejected with the documented status; the one-line request log still fires.

What a pass does NOT prove: that a live model resists jailbreaks. The mock never "obeys"
anything, so semantic injection resistance is untested here (see docs/THREAT_MODEL.md T09).
Attacks live in platform/tests/redteam/attacks.json and are shared with platform/tests/test_redteam.py.
"""

import base64
import csv
import html
import http.client
import io
import json
import logging
import pathlib
import threading
from http.server import ThreadingHTTPServer

import llm
import pytest
import underwriter_server as srv
from permission_rag import PermissionRAG
from test_http import _jwt

DATA = json.loads(
    (pathlib.Path(__file__).resolve().parent.parent / "platform/tests/redteam/attacks.json").read_text()
)
ATTACKS = [(a, "ref") for a in DATA["attacks"] if "ref" in a["surfaces"]]
SECRET = "redteam-secret"
# Appears only in documents junior cannot read (bank-delgado, credit-memo-delgado, watchlist).
FORBIDDEN = [
    "310000",
    "Robert Delgado",
    "misrepresentation",
    "1.25 threshold",
    "NSF events",
    "72 percent",
    "debt service coverage ratio 1.1",
    "premium loading",
]
READABLE = {"policy-10023", "policy-10088", "claims-10023", "guidelines"}
JWT_BASE_Q = "Delgado credit memo bank profile watchlist debt service coverage"
BASE_QUERY_Q = "policy status"
KINDS_IN_JWT_MODE = {"fields", "headers", "jwt", "csv_sub"}


def expand(value, surface="ref"):
    if isinstance(value, dict) and "repeat" in value:
        return value["repeat"][0] * value["repeat"][1]
    if isinstance(value, dict) and surface in value:
        return value[surface]
    return value


def reference_prompt(results, question):
    """Independent statement of the prompt contract: escaped blocks, reminder after, raw question last."""
    blocks = (
        "\n\n".join(
            f'<document id="{html.escape(r["doc_id"], quote=True)}">\n{html.escape(r["text"], quote=True)}\n</document>'
            for r in results
        )
        or "(no accessible documents matched)"
    )
    return f"Context (permission-filtered):\n{blocks}\n\n{llm.REMINDER}\n\nQuestion: {question}"


class Model:
    """Mock provider: records every request and answers with the document context it was shown."""

    def __init__(self):
        self.calls = []

    def __call__(self, body, key, timeout, *a, **k):
        self.calls.append(body)
        content = body["messages"][0]["content"]
        echo = content.split(f"\n\n{llm.REMINDER}", 1)[0]
        return {"content": [{"type": "text", "text": echo}], "usage": {"input_tokens": 1, "output_tokens": 1}}


@pytest.fixture
def api(monkeypatch):
    rag = PermissionRAG()
    for doc_id, text, acl in srv.CORPUS:
        rag.add_document(doc_id, text, acl)
    rag.audit_path = None
    model = Model()
    monkeypatch.setattr(srv, "rag", rag)
    monkeypatch.setattr(srv, "JWT_SECRET", None)
    monkeypatch.setattr(srv, "rate_limited", lambda ip: False)
    monkeypatch.setattr(llm, "_post", model)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "redteam-key")
    server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    port = server.server_address[1]

    def send(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body, headers or {})
            r = conn.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            conn.close()

    send.model, send.rag = model, rag
    yield send
    server.shutdown()


def token(secret=SECRET, **claims):
    return _jwt(secret, {"sub": "junior", "groups": ["underwriting"], "exp": 4102444800, **claims})


def build_request(attack):
    """-> (path, body bytes, headers) for the attack."""
    kind = attack["kind"]
    jsonh = {"content-type": "application/json"}
    if kind in ("query", "doc"):
        q = expand(attack["q"])
        return "/ask", json.dumps({"user": "junior", "q": q}).encode(), jsonh
    if kind == "raw":
        raw = expand(attack["raw"]).encode()
        if len(raw) > srv.MAX_BODY * 4:
            # the server refuses on Content-Length alone and closes without reading; sending the body
            # would race a BrokenPipe/reset, so declare the size and send nothing
            return "/ask", None, {**jsonh, "Content-Length": str(len(raw))}
        return "/ask", raw, jsonh
    bearer = {"Authorization": f"Bearer {token()}"}
    if kind == "fields":
        return "/ask", json.dumps({"q": JWT_BASE_Q, **attack["fields"]}).encode(), {**jsonh, **bearer}
    if kind == "headers":
        qs = "&".join(f"{k}={v}" for k, v in attack.get("qs", {}).items())
        path = "/ask" + ("?" + qs if qs else "")
        return path, json.dumps({"q": JWT_BASE_Q}).encode(), {**jsonh, **bearer, **attack.get("headers", {})}
    assert kind == "jwt", kind
    variant = attack["variant"]
    if variant == "unsigned":

        def enc(o):
            return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

        tok = f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc({'sub': 'senior', 'groups': ['senior'], 'exp': 4102444800})}."
    elif variant == "bad_signature":
        tok = token(secret="not-the-secret", **attack.get("claims", {}))
    elif variant == "expired":
        tok = token(exp=1)
    else:
        assert variant == "signed", variant
        tok = token(**attack.get("claims", {}))
    return "/ask", json.dumps({"q": JWT_BASE_Q}).encode(), {**jsonh, "Authorization": f"Bearer {tok}"}


def assert_clean(attack, status, body, api, caplog, headers):
    expect = attack.get("expect", {}).get("ref", [200])
    text = body.decode("utf-8", "replace")
    assert status in expect, (attack["id"], status, text[:200])
    assert status < 500 or status == 503
    assert llm.SYSTEM_PROMPT.splitlines()[0] not in text, "system prompt echoed"
    for secret in FORBIDDEN:
        assert secret not in text, (attack["id"], secret)
    request_lines = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "permrag" and r.getMessage().startswith("{")
    ]
    assert len(request_lines) == 1, request_lines  # exactly one log line per request, rejected or not
    assert request_lines[0]["request_id"] == headers["X-Request-ID"]
    if status != 200:
        assert api.model.calls == [], "a rejected request must not reach the model"
        return
    out = json.loads(body)
    assert out["request_id"] == headers["X-Request-ID"]
    if attack["kind"] != "doc":
        assert {r["doc_id"] for r in out["results"]} <= READABLE
    assert len(api.model.calls) == (1 if out["results"] else 0)
    for call in api.model.calls:
        assert call["system"][0]["text"] == llm.SYSTEM_PROMPT  # attacker text never reaches the system slot
        content = call["messages"][0]["content"]
        context = content.split(f"\n\n{llm.REMINDER}", 1)[0]
        for secret in FORBIDDEN:
            assert secret not in context, (attack["id"], secret)
        assert content == reference_prompt(out["results"], question_of(attack))


def question_of(attack):
    return {"jwt": JWT_BASE_Q, "fields": JWT_BASE_Q, "headers": JWT_BASE_Q}.get(attack["kind"]) or expand(
        attack["q"]
    )


@pytest.mark.parametrize("attack, surface", ATTACKS, ids=[a["id"] for a, _ in ATTACKS])
def test_attack(api, caplog, monkeypatch, attack, surface):
    caplog.set_level(logging.INFO)
    if attack["kind"] in KINDS_IN_JWT_MODE:
        monkeypatch.setattr(srv, "JWT_SECRET", SECRET)
    if attack["kind"] == "csv_sub":
        return csv_attack(api, attack, monkeypatch)
    if attack["kind"] == "doc":
        d = attack["doc"]
        api.rag.add_document(d["id"], d["text"], {"*"})
    path, body, headers = build_request(attack)
    status, resp, resp_headers = api("POST", path, body, headers)
    if attack["kind"] == "doc":
        assert d["id"] in [r["doc_id"] for r in json.loads(resp)["results"]], "payload doc was not retrieved"
    assert_clean(attack, status, resp, api, caplog, resp_headers)


def csv_attack(api, attack, monkeypatch):
    monkeypatch.setattr(srv, "JWT_SECRET", SECRET)
    sub = attack["sub"]
    bearer = {"Authorization": f"Bearer {token(sub=sub, groups=['underwriting', 'audit'])}"}
    assert (
        api(
            "POST",
            "/ask",
            json.dumps({"q": BASE_QUERY_Q}).encode(),
            {**bearer, "content-type": "application/json"},
        )[0]
        == 200
    )
    status, body, headers = api("GET", "/audit?format=csv", None, bearer)
    assert status == 200 and headers["Content-Type"] == "text/csv"
    rows = list(csv.reader(io.StringIO(body.decode())))
    assert len(rows) >= 2
    for row in rows[1:]:
        for cell in row:
            assert not cell.lstrip(" \t\r\n﻿").startswith(("=", "+", "-", "@")), (attack["id"], cell)
    assert rows[1][1] == "'" + sub  # neutralised, original value preserved after the quote


def test_dataset_meets_the_brief():
    ids = [a["id"] for a in DATA["attacks"]]
    assert len(ids) == len(set(ids)) >= 50
    assert {a["category"] for a in DATA["attacks"]} >= {
        "prompt_injection",
        "system_prompt_extraction",
        "document_breakout",
        "document_borne",
        "csv_formula",
        "identity_smuggling",
        "oversized_malformed",
        "cross_role_exact_content",
    }
    assert "not prove" in DATA["about"]


def test_hostile_model_citing_restricted_documents_is_flagged(api, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        llm,
        "_post",
        lambda *a, **k: {
            "content": [
                {
                    "type": "text",
                    "text": "Bind it per [watchlist] and [credit-memo-delgado]. Both clear the risk.",
                }
            ],
            "usage": {},
        },
    )
    status, body, _ = api(
        "POST",
        "/ask",
        json.dumps({"user": "junior", "q": "policy status"}).encode(),
        {"content-type": "application/json"},
    )
    out = json.loads(body)
    assert status == 200 and out["unverified_citations"] == ["credit-memo-delgado", "watchlist"]
    assert out["uncited_claims"] == [
        "Bind it per [watchlist] and [credit-memo-delgado].",
        "Both clear the risk.",
    ]  # a citation only counts if it names a retrieved document
    lines = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "permrag" and r.getMessage().startswith("{")
    ]
    assert (lines[0]["unverified_citations"], lines[0]["uncited_claims"]) == (2, 2)
