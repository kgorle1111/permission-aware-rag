"""Permission-Aware RAG API.

POST /query    — JWT-verified, ACL pre-filtered retrieval + grounded answer
GET  /audit    — admin only (group:security): trail + denied-query heatmap
POST /sync     — trigger a permission reconciliation pass (webhook target)
GET  /         — demo UI (three users, one query, three different answers)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import audit as audit_mod
from . import config
from .generation import answer
from .identity import Principal, principal_from_request
from .retrieval import retrieve
from .store import init_db
from .sync import sync_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("permrag")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Permission-Aware RAG", lifespan=lifespan)


class QueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=0, ge=0, le=20)


@app.post("/query")
def query(body: QueryIn, principal: Principal = Depends(principal_from_request)):
    resp = retrieve(body.query, principal, k=body.k or None)
    if resp.get("answer") is None:
        resp["answer"] = answer(body.query, resp["results"])
    return resp


@app.get("/audit")
def audit(principal: Principal = Depends(principal_from_request)):
    if "security" not in principal.groups:
        raise HTTPException(403, "audit access requires group:security")
    return {"recent": audit_mod.recent(), "denied_heatmap": audit_mod.denied_heatmap()}


@app.post("/sync")
def sync(principal: Principal = Depends(principal_from_request)):
    if "security" not in principal.groups:
        raise HTTPException(403, "sync trigger requires group:security")
    return {"changed_docs": sync_once()}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def demo_ui():
    return HTMLResponse((Path(__file__).parent.parent / "static" / "index.html").read_text())


@app.get("/demo/tokens")
def demo_tokens():
    """DEV ONLY (DEMO_MODE=1): pre-minted tokens for the demo UI's user picker.
    In any real deployment users get tokens from the IdP, never from the API."""
    if not config.DEMO_MODE:
        raise HTTPException(404, "not found")
    import scripts.mint_token as mint
    return {name: mint.mint(user, groups) for name, (user, groups) in {
        "alice (eng)": ("alice@company.com", ["eng"]),
        "bob (hr)": ("bob@company.com", ["hr"]),
        "ceo (board)": ("ceo@company.com", ["board", "exec"]),
        "guest": ("guest@external.com", []),
        "secops (security)": ("sec@company.com", ["security"]),
    }.items()}
