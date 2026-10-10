"""Permission-Aware RAG API.

POST /query    — JWT-verified, ACL pre-filtered retrieval + grounded answer
GET  /audit    — admin only (group:security): trail + denied-query heatmap
POST /sync     — security-admin permission reconciliation
POST /webhooks/drive — channel-authenticated Drive notifications
GET  /         — demo UI (three users, one query, three different answers)
"""
from __future__ import annotations

import logging
import asyncio
import hmac
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import audit as audit_mod
from . import config
from . import observability
from .identity import Principal, principal_from_request
from .retrieval import retrieve
from .store import init_db, SessionLocal, PermissionState, fingerprint_problem
from .sync import sync_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("permrag")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    stop = asyncio.Event()
    async def poll():
        while not stop.is_set():
            try:
                await asyncio.to_thread(sync_once)
            except Exception:
                log.exception("permission sync failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.SYNC_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
    # Reconcile before accepting traffic so startup cannot serve stale corpus ACLs.
    if config.SYNC_INTERVAL_S > 0:
        try:
            await asyncio.to_thread(sync_once)
        except Exception:
            log.exception("startup reconciliation failed; retrieval remains blocked")
    task = asyncio.create_task(poll()) if config.SYNC_INTERVAL_S > 0 else None
    try:
        yield
    finally:
        if task is not None:
            stop.set()
            await task


app = FastAPI(title="Permission-Aware RAG", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """422 without echoing the rejected input: the default handler returns it verbatim, and a lone
    surrogate in it cannot be encoded, which turned a bad request into a 500."""
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]})


class QueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=0, ge=0, le=20)

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value):
        if not value.strip():
            raise ValueError("query must contain non-whitespace text")
        return value


@app.middleware("http")
async def request_id_and_rejections(request: Request, call_next):
    """X-Request-ID on every response (a header, not a body field: fail-closed and no-match
    bodies must stay byte-identical). /query requests that never reach retrieval (401, 422, 405)
    get their one log line here; retrieve() logs the ones that do (it always answers 200)."""
    request_id, started = uuid.uuid4().hex, time.perf_counter()
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if request.url.path == "/query" and response.status_code != 200:
        rec = observability.new_record(request_id)
        rec["outcome"] = "bad_request"
        rec["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        observability.OPS.record(rec)
        observability.emit(rec)
    return response


@app.post("/query")
def query(request: Request, body: QueryIn, principal: Principal = Depends(principal_from_request)):
    return retrieve(body.query, principal, k=body.k or None, request_id=request.state.request_id)


@app.get("/audit")
def audit(principal: Principal = Depends(principal_from_request)):
    if "security" not in principal.groups:
        raise HTTPException(403, "audit access requires group:security")
    return {"recent": audit_mod.recent(), "denied_heatmap": audit_mod.denied_heatmap(),
            "ops": observability.OPS.summary()}


@app.post("/sync")
def sync(principal: Principal = Depends(principal_from_request)):
    if "security" not in principal.groups:
        raise HTTPException(403, "sync trigger requires group:security")
    try:
        return {"changed_docs": sync_once()}
    except Exception:
        log.exception("manual permission sync failed")
        raise HTTPException(503, "permission reconciliation unavailable")


@app.post("/webhooks/drive", status_code=204)
def drive_notification(request: Request):
    # Google notification delivery cannot use our end-user JWT. It must match
    # the pre-registered channel capability and resource before triggering sync.
    expected = {
        "x-goog-channel-id": config.DRIVE_WEBHOOK_CHANNEL_ID,
        "x-goog-channel-token": config.DRIVE_WEBHOOK_TOKEN,
        "x-goog-resource-id": config.DRIVE_WEBHOOK_RESOURCE_ID,
    }
    if config.PERMISSIONS_BACKEND != "gdrive" or not all(expected.values()):
        raise HTTPException(404, "not found")
    if not all(hmac.compare_digest(request.headers.get(name, "").encode(), value.encode())
               for name, value in expected.items()):
        raise HTTPException(403, "invalid notification channel")
    try:
        sync_once()
    except Exception:
        log.exception("Drive notification reconciliation failed")
        raise HTTPException(503, "permission reconciliation unavailable")
    return Response(status_code=204)


@app.get("/readyz")
def readyz():
    try:
        with SessionLocal() as session:
            state = session.get(PermissionState, 1)
            if state and not state.pending and not state.rebuild_required:
                problem = fingerprint_problem(session)
                if problem is None:
                    return {"ready": True}
                log.error("not ready: %s", problem)
    except Exception:
        log.exception("readiness check failed")
    raise HTTPException(503, "permission reconciliation required")


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
