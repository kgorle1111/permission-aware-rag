"""Test bootstrap: isolated temp Qdrant + SQLite + a throwaway IdP keypair.

Env is set BEFORE app modules import. Every test runs fully offline.
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp = Path(tempfile.mkdtemp(prefix="permrag-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/rag.db"
os.environ["QDRANT_PATH"] = str(_tmp / "qdrant")
os.environ["JWT_PRIVATE_KEY_PATH"] = str(_tmp / "idp_private.pem")
os.environ["JWT_PUBLIC_KEY_PATH"] = str(_tmp / "idp_public.pem")
os.environ["PERMISSIONS_SOURCE"] = str(_tmp / "permissions.jsonl")
if os.environ.get("RUN_LIVE_CONTRACT") == "1":  # read only by test_live_contract.py
    os.environ["_LIVE_CONTRACT_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")
os.environ["ANTHROPIC_API_KEY"] = ""  # extractive answers only — offline
os.environ["DEMO_MODE"] = "0"
os.environ["SYNC_INTERVAL_S"] = "0"
os.environ["PERMISSIONS_BACKEND"] = "jsonl"
os.environ["QDRANT_URL"] = ""
os.environ["JWKS_URL"] = ""
os.environ["JWT_ISSUER"] = ""
os.environ["EMBED_BACKEND"] = "hash"
os.environ["EMBED_DIM"] = "384"

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Keep the signing key stable if mutmut loads this conftest under more than
# one module name within a pytest process. Older test helpers can remain bound
# to the first module while later tests import mint from a reloaded conftest.
_key_holder = sys.modules.get("_permrag_test_key")
if _key_holder is None:
    _key_holder = types.SimpleNamespace(
        key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    sys.modules["_permrag_test_key"] = _key_holder
_key = _key_holder.key

# A reloaded conftest can see an already-cached app.config whose paths still
# point at the first pytest session's temp directory. Write the same key to
# those active paths so both old and newly imported helpers mint valid tokens.
_cached_config = sys.modules.get("app.config")
_private_path = getattr(_cached_config, "JWT_PRIVATE_KEY_PATH", os.environ["JWT_PRIVATE_KEY_PATH"])
_public_path = getattr(_cached_config, "JWT_PUBLIC_KEY_PATH", os.environ["JWT_PUBLIC_KEY_PATH"])
Path(_private_path).write_bytes(_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()))
Path(_public_path).write_bytes(_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

from app import config  # noqa: E402
from app.ingest import ingest_corpus  # noqa: E402
from app.main import app  # noqa: E402
from app.retrieval import clear_cache  # noqa: E402
from app.store import AuditLog, SessionLocal, init_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus" / "docs.json"

CANARIES = {
    "eng-oncall": "ENG-CANARY-7f3a9",
    "hr-salaries": "HR-CANARY-2b8c1",
    "exec-comp": "EXEC-CANARY-9d4e2",
    "board-minutes": "BOARD-CANARY-5a6f0",
    "company-strategy(finance section)": "FIN-CANARY-4c1d7",
}

def forbidden_canaries(name: str) -> list[str]:
    """The canary strings this user must NEVER see. (Seeing a canary from a
    doc they're authorized for is not a leak.)"""
    sub, groups, readable = USERS[name]
    out = []
    if "eng-oncall" not in readable:
        out.append(CANARIES["eng-oncall"])
    if "hr-salaries" not in readable:
        out.append(CANARIES["hr-salaries"])
    if "exec-comp" not in readable:
        out.append(CANARIES["exec-comp"])
    if "board-minutes" not in readable:
        out.append(CANARIES["board-minutes"])
    if "finance" not in groups:
        out.append(CANARIES["company-strategy(finance section)"])
    return out


# user -> (sub, groups, docs they may read)
USERS = {
    "alice": ("alice@company.com", ["eng"], {"handbook", "eng-oncall", "company-strategy"}),
    "bob": ("bob@company.com", ["hr"], {"handbook", "hr-salaries", "company-strategy"}),
    "carol": ("carol@company.com", ["finance"], {"handbook", "company-strategy"}),
    "ceo": ("ceo@company.com", ["board", "exec"], {"handbook", "exec-comp", "board-minutes", "company-strategy"}),
    "guest": ("guest@external.com", [], {"handbook", "company-strategy"}),
}


def mint(user: str, groups: list[str], ttl_s: int = 3600, aud: str = None,
         key=None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return pyjwt.encode(
        {"sub": user, "groups": groups, "aud": aud or config.JWT_AUDIENCE,
         "iat": now, "exp": now + dt.timedelta(seconds=ttl_s)},
        key or _key, algorithm="RS256")


def token_for(name: str) -> str:
    sub, groups, _ = USERS[name]
    return mint(sub, groups)


def auth(name: str) -> dict:
    return {"Authorization": f"Bearer {token_for(name)}"}


def reset_permission_source():
    src = json.loads(CORPUS.read_text())
    lines = [json.dumps({"doc_id": d["doc_id"], "acl": d["acl"]}) for d in src]
    Path(config.PERMISSIONS_SOURCE).write_text("\n".join(lines) + "\n")


@pytest.fixture(scope="session")
def ingested():
    init_db()
    reset_permission_source()
    n = ingest_corpus(CORPUS)
    assert n > 0
    return n


@pytest.fixture()
def client(ingested):
    # Mutation runners select and reorder tests. Each real API test must start
    # with independent permission/index state, not inherit a previous test.
    reingest()
    clear_cache()
    with SessionLocal() as s:
        s.query(AuditLog).delete()
        s.commit()
    with TestClient(app) as c:
        yield c


def reingest():
    """Restore corpus + permissions after tests that mutate ACLs."""
    from app.sync import sync_once
    reset_permission_source()
    ingest_corpus(CORPUS)
    sync_once()
    clear_cache()
