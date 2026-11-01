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
from .identity import Principal, principal_from_request, validate_identity_config
from .retrieval import retrieve_checked
from .store import init_db, SessionLocal, PermissionState, fingerprint_problem
from .sync import sync_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("permrag")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_identity_config()  # first: refuse to boot rather than 401 every request
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

# The largest legitimate body is a QueryIn (2000-char query): 64 KB leaves headroom for 4-byte UTF-8
# and JSON escapes. Pydantic's max_length only runs after the whole body is buffered.
MAX_BODY_BYTES = 64 * 1024


class BodySizeLimit:
    """413 for a body over MAX_BODY_BYTES before the app buffers it: a declared Content-Length is
    checked up front, and a chunked body is counted as it streams and cut off at the cap."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
            return await self._respond(413, "request body too large", scope, receive, send)
        # kn: holds up to 64 KB per in-flight request (x --limit-concurrency 64 = 4 MB); stream
        # through instead if a route ever needs bodies larger than that
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                # client left mid-body: answer anyway (the server drops it) so the request-id
                # middleware gets a response instead of raising "No response returned"
                return await self._respond(400, "client disconnected", scope, receive, send)
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_BODY_BYTES:
                return await self._respond(413, "request body too large", scope, receive, send)
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _respond(status, detail, scope, receive, send):
        await JSONResponse(status_code=status, content={"detail": detail})(scope, receive, send)


# registered before request_id_and_rejections so it runs inside it: a 413 still gets X-Request-ID
app.add_middleware(BodySizeLimit)


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
    """X-Request-ID on every response (a header; the no-match body must stay byte-identical to a
    denied one). /query requests that never reach retrieval (401, 422, 405) get their one log line
    here; retrieve() logs the ones that do (200, or 503 when failed closed)."""
    request_id, started = uuid.uuid4().hex, time.perf_counter()
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if request.url.path == "/query" and response.status_code not in (200, 503):
        rec = observability.new_record(request_id)
        rec["outcome"] = "bad_request"
        rec["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        observability.OPS.record(rec)
        observability.emit(rec)
    return response


@app.post("/query")
def query(request: Request, body: QueryIn, principal: Principal = Depends(principal_from_request)):
    response, failed_closed = retrieve_checked(
        body.query, principal, k=body.k or None, request_id=request.state.request_id)
    if failed_closed:
        # Fixed body: no cause, no query echo. The cause is in the log and the audit row.
        return JSONResponse(status_code=503, content={
            "error": "retrieval unavailable", "request_id": request.state.request_id})
    return response


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
