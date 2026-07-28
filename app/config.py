"""Central config for Permission-Aware RAG."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'rag.db'}")

# Qdrant: embedded local mode by default (zero infra). Set QDRANT_URL for a server.
QDRANT_URL = os.getenv("QDRANT_URL", "")
QDRANT_PATH = os.getenv("QDRANT_PATH", str(ROOT / "qdrant_data"))
COLLECTION = os.getenv("COLLECTION", "chunks")

EMBED_DIM = int(os.getenv("EMBED_DIM", "384"))
# hash = built-in hashing-trick embedder (offline, deterministic)
# st   = sentence-transformers BAAI/bge-small-en-v1.5 (pip install sentence-transformers)
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "hash")

# Identity: RS256 public key for verifying JWTs. scripts/gen_keys.py creates a
# local IdP keypair; in production point JWKS_URL at your IdP instead.
JWT_PUBLIC_KEY_PATH = os.getenv("JWT_PUBLIC_KEY_PATH", str(ROOT / "config" / "idp_public.pem"))
JWT_PRIVATE_KEY_PATH = os.getenv("JWT_PRIVATE_KEY_PATH", str(ROOT / "config" / "idp_private.pem"))
JWKS_URL = os.getenv("JWKS_URL", "")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "permission-rag-api")

TOP_K = int(os.getenv("TOP_K", "4"))
# similarity floor: hits below this never count as results, so a query about a
# topic the user can't read looks exactly like a query about nothing
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.1"))

# Optional LLM generation (extractive answers when unset — demo runs keyless)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
GENERATION_MODEL = os.getenv("GENERATION_MODEL", "claude-haiku-4-5")

# Permission source of truth for the sync loop (synthetic stand-in for
# Drive/Confluence; swap in the Drive connector for the real thing)
PERMISSIONS_SOURCE = os.getenv("PERMISSIONS_SOURCE", str(ROOT / "corpus" / "permissions.jsonl"))

# Answer cache TTL (cache keys are permission-scoped — tested)
CACHE_TTL_S = float(os.getenv("CACHE_TTL_S", "300"))

DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"
