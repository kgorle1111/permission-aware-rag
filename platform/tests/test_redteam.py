"""Red-team suite against the real HTTP surface (FastAPI TestClient), with a MOCKED model.

What a pass proves: structural properties. The ACL filter runs inside the vector query before
the model is involved, so restricted text never reaches the prompt; retrieved text is escaped
inside <document> blocks; the system prompt is untouched; identity comes only from the verified
JWT; malformed and oversized input gets the documented status; one request log line still fires.

What a pass does NOT prove: that a live model resists jailbreaks. The mock never "obeys"
anything, so semantic injection resistance is untested here (see docs/THREAT_MODEL.md T09).
Attacks live in evals/redteam/attacks.json and are shared with app/test_redteam.py.
"""
import base64
import html
import json
import logging
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import config, generation, ingest, resilience, retrieval
from app.sync import sync_once

from conftest import CORPUS, _key, auth, forbidden_canaries, mint, reingest

DATA = json.loads((Path(__file__).resolve().parents[2] / "evals/redteam/attacks.json").read_text())
ATTACKS = [a for a in DATA["attacks"] if "platform" in a["surfaces"]]
FORBIDDEN = [*forbidden_canaries("alice"), "Band adjustments happen every April", "Northwind Labs",
             "$1.2M", "71 percent", "$48M"]
READABLE = {"handbook", "eng-oncall", "company-strategy"}
BASE_Q = "salary bands compensation board minutes executive compensation"
SYSTEM_MARKER = generation.SYSTEM.split(".")[0]


def expand(value):
    if isinstance(value, dict) and "repeat" in value:
        return value["repeat"][0] * value["repeat"][1]
    if isinstance(value, dict) and "platform" in value:
        return value["platform"]
    return value


def reference_prompt(results, question):
    """Independent statement of the prompt contract: escaped blocks, reminder after, raw question last."""
    blocks = "\n\n".join(
        f'<document id="{html.escape(r["doc_id"], quote=True)}">\n{html.escape(r["text"], quote=True)}\n</document>'
        for r in results)
    return f"Context:\n{blocks}\n\n{generation.REMINDER}\n\nQuestion: {question}"


@pytest.fixture
def model(monkeypatch):
    """Mock provider: records every request and answers with the document context it was shown."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "redteam-key")
    monkeypatch.setattr(generation.time, "sleep", lambda s: None)
    calls = []

    def post(url, **kw):
        calls.append(kw["json"])
        content = kw["json"]["messages"][0]["content"]
        echo = content.split(f"\n\n{generation.REMINDER}", 1)[0]
        return httpx.Response(200, json={"content": [{"text": echo}], "usage": {}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(generation.httpx, "post", post)
    resilience.reset_breakers()
    yield calls
    resilience.reset_breakers()


def token(claims=None, key=None, **kw):
    if claims is None and key is None and not kw:
        return mint("alice@company.com", ["eng"])
    base = {"sub": "alice@company.com", "groups": ["eng"], "aud": config.JWT_AUDIENCE, "exp": 4102444800}
    return pyjwt.encode({**base, **(claims or {})}, key or _key, algorithm="RS256")


def post(client, payload, headers, params=None):
    # ensure_ascii escapes lone surrogates, which httpx's json= argument refuses to encode
    return client.post("/query", params=params, content=json.dumps(payload).encode(),
                       headers={**headers, "content-type": "application/json"})


def send(client, attack):
    kind = attack["kind"]
    headers = {"Authorization": f"Bearer {token()}"}
    if kind in ("query", "doc"):
        return post(client, {"query": expand(attack["q"])}, headers)
    if kind == "raw":
        return client.post("/query", content=expand(attack["raw"]).encode(),
                           headers={**headers, "content-type": "application/json"})
    if kind == "fields":
        return post(client, {"query": BASE_Q, **attack["fields"]}, headers)
    if kind == "headers":
        return post(client, {"query": BASE_Q}, {**headers, **attack.get("headers", {})}, attack.get("qs"))
    assert kind == "jwt", kind
    variant = attack["variant"]
    if variant == "unsigned":
        enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
        tok = f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc({'sub': 'ceo@company.com', 'groups': ['board'], 'aud': config.JWT_AUDIENCE, 'exp': 4102444800})}."
    elif variant == "bad_signature":
        tok = token(attack.get("claims"), key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    elif variant == "expired":
        tok = token({"exp": 1})
    elif variant == "wrong_audience":
        tok = token({"aud": "some-other-api"})
    else:
        assert variant == "signed", variant
        tok = token(attack.get("claims"))
    return post(client, {"query": BASE_Q}, {"Authorization": f"Bearer {tok}"})


def ingest_poisoned(tmp_path, doc):
    docs = json.loads(CORPUS.read_text()) + [
        {"doc_id": doc["id"], "acl": ["*"], "sections": [{"text": doc["text"]}]}]
    path = tmp_path / "docs.json"
    path.write_text(json.dumps(docs))
    Path(config.PERMISSIONS_SOURCE).write_text(
        "\n".join(json.dumps({"doc_id": d["doc_id"], "acl": d["acl"]}) for d in docs) + "\n")
    ingest.ingest_corpus(path)
    sync_once()
    retrieval.clear_cache()


def question_of(attack):
    return BASE_Q if attack["kind"] in ("jwt", "fields", "headers") else expand(attack["q"])


@pytest.mark.parametrize("attack", ATTACKS, ids=[a["id"] for a in ATTACKS])
def test_attack(client, model, caplog, tmp_path, attack):
    caplog.set_level(logging.INFO)
    if attack["kind"] == "doc":
        ingest_poisoned(tmp_path, attack["doc"])
    try:
        r = send(client, attack)
    finally:
        if attack["kind"] == "doc":
            reingest()
    expect = attack.get("expect", {}).get("platform", [200])
    assert r.status_code in expect, (attack["id"], r.status_code, r.text[:200])
    assert r.status_code < 500
    assert SYSTEM_MARKER not in r.text, "system prompt echoed"
    for secret in FORBIDDEN:
        assert secret not in r.text, (attack["id"], secret)
    lines = [json.loads(x.getMessage()) for x in caplog.records
             if x.name == "permrag" and x.getMessage().startswith("{")]
    assert len(lines) == 1, lines  # exactly one log line per /query request, rejected or not
    assert lines[0]["request_id"] == r.headers["X-Request-ID"]
    if r.status_code != 200:
        assert model == [] and lines[0]["outcome"] == "bad_request"
        return
    out = r.json()
    if attack["kind"] == "doc":
        assert attack["doc"]["id"] in [x["doc_id"] for x in out["results"]], "payload doc was not retrieved"
    else:
        assert {x["doc_id"] for x in out["results"]} <= READABLE
    assert len(model) == (1 if out["results"] else 0)
    for call in model:
        assert call["system"] == generation.SYSTEM  # attacker text never reaches the system slot
        content = call["messages"][0]["content"]
        for secret in FORBIDDEN:
            assert secret not in content.split(f"\n\n{generation.REMINDER}", 1)[0], (attack["id"], secret)
        assert content == reference_prompt(out["results"], question_of(attack))


def test_dataset_meets_the_brief():
    ids = [a["id"] for a in DATA["attacks"]]
    assert len(ids) == len(set(ids)) >= 50
    assert {a["category"] for a in DATA["attacks"]} >= {
        "prompt_injection", "system_prompt_extraction", "document_breakout", "document_borne",
        "csv_formula", "identity_smuggling", "oversized_malformed", "cross_role_exact_content"}
    assert "not prove" in DATA["about"]


def test_hostile_model_output_is_still_redacted_and_cannot_widen_results(client, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "redteam-key")
    text = "Per [hr-salaries] and [exec-comp] call 555-123-4567 or mail boss@company.com."
    monkeypatch.setattr(generation.httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"content": [{"text": text}], "usage": {}}, request=httpx.Request("POST", url)))
    r = client.post("/query", json={"query": "vacation policy"}, headers=auth("alice"))
    body = r.json()
    assert "555-123-4567" not in body["answer"] and "boss@company.com" not in body["answer"]
    assert {x["doc_id"] for x in body["results"]} <= READABLE
    line = [json.loads(x.getMessage()) for x in caplog.records if x.getMessage().startswith("{")][0]
    assert line["unverified_citations"] == 2


def test_validation_errors_are_422_and_never_echo_the_rejected_input(client):
    lone = post(client, {"query": "policy \ud800 status"}, auth("alice"))
    assert lone.status_code == 422  # was a 500: the default handler echoed the unencodable input
    assert [(e["loc"], e["type"]) for e in lone.json()["detail"]] == [(["body", "query"], "string_unicode")]
    short = post(client, {"query": ""}, auth("alice"))
    assert short.status_code == 422
    assert [(e["loc"], e["type"]) for e in short.json()["detail"]] == [(["body", "query"], "string_too_short")]
    assert all(set(e) == {"loc", "msg", "type"} for e in short.json()["detail"])
    secret = post(client, {"query": "x", "k": "my-private-text"}, auth("alice"))
    assert secret.status_code == 422 and "my-private-text" not in secret.text
