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
# fastembed = ONNX BAAI/bge-small-en-v1.5, no PyTorch (pip install -r requirements-models.txt)
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "hash")

# Identity: RS256 public key for verifying JWTs. scripts/gen_keys.py creates a
# local IdP keypair; in production point JWKS_URL at your IdP instead.
JWT_PUBLIC_KEY_PATH = os.getenv("JWT_PUBLIC_KEY_PATH", str(ROOT / "config" / "idp_public.pem"))
JWT_PRIVATE_KEY_PATH = os.getenv("JWT_PRIVATE_KEY_PATH", str(ROOT / "config" / "idp_private.pem"))
JWKS_URL = os.getenv("JWKS_URL", "")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "permission-rag-api")

JWT_ISSUER = os.getenv("JWT_ISSUER", "")
SYNC_INTERVAL_S = float(os.getenv("SYNC_INTERVAL_S", "10"))
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "256"))

TOP_K = int(os.getenv("TOP_K", "4"))
# similarity floor: hits below this never count as results, so a query about a
# topic the user can't read looks exactly like a query about nothing
# Default floors are per embedder: bge cosine similarity between unrelated texts sits around
# 0.43-0.54 (measured, evals/results/2026-10-10-platform-rungs), so the hash embedder's 0.1 would pass everything.
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.55" if EMBED_BACKEND in {"st", "fastembed"} else "0.1"))

# Optional LLM generation (extractive answers when unset — demo runs keyless)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
GENERATION_MODEL = os.getenv("GENERATION_MODEL", "claude-haiku-4-5-20251001")
# used once, only when the primary id returns 404 not_found_error (retired model)
GENERATION_FALLBACK_MODEL = os.getenv("GENERATION_FALLBACK_MODEL", "claude-haiku-4-5")

# Permission source of truth for the sync loop (synthetic stand-in for
# Drive/Confluence; swap in the Drive connector for the real thing)
PERMISSIONS_SOURCE = os.getenv("PERMISSIONS_SOURCE", str(ROOT / "corpus" / "permissions.jsonl"))

# Answer cache TTL (cache keys are permission-scoped — tested)
CACHE_TTL_S = float(os.getenv("CACHE_TTL_S", "300"))

DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"

# Optional real Drive source. Corpus doc_ids must be native Drive file IDs.
PERMISSIONS_BACKEND = os.getenv("PERMISSIONS_BACKEND", "jsonl")
DRIVE_CREDENTIALS_FILE = os.getenv("DRIVE_CREDENTIALS_FILE", "")
DRIVE_SUBJECT = os.getenv("DRIVE_SUBJECT", "")
DRIVE_WEBHOOK_CHANNEL_ID = os.getenv("DRIVE_WEBHOOK_CHANNEL_ID", "")
DRIVE_WEBHOOK_TOKEN = os.getenv("DRIVE_WEBHOOK_TOKEN", "")
DRIVE_WEBHOOK_RESOURCE_ID = os.getenv("DRIVE_WEBHOOK_RESOURCE_ID", "")

# Embedding model identity recorded in the index fingerprint ("" = backend default)
EMBED_MODEL = os.getenv("EMBED_MODEL", "")
# Circuit breakers (embedder / vector store / LLM): consecutive failures to open, seconds to half-open
BREAKER_THRESHOLD = int(os.getenv("BREAKER_THRESHOLD", "3"))
BREAKER_RESET_S = float(os.getenv("BREAKER_RESET_S", "30"))

# Local-model rungs (all need requirements-models.txt; defaults keep the keyless hash path).
# Learned-sparse hybrid: "none" or "splade". Never enable Qdrant's IDF modifier (S1).
SPARSE_BACKEND = os.getenv("SPARSE_BACKEND", "none")
SPARSE_MODEL = os.getenv("SPARSE_MODEL", "")
# Cross-encoder rerank of ACL-filtered candidates: "none" or "fastembed".
RERANKER = os.getenv("RERANKER", "none")
RERANK_MODEL = os.getenv("RERANK_MODEL", "")
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "25"))
# Raw cross-encoder logit floor, calibrated for the default ms-marco MiniLM model on 10 out-of-corpus
# queries (just above their best score). It also drops paraphrase targets: see the rung table.
RERANK_MIN_SCORE = float(os.getenv("RERANK_MIN_SCORE", "-10.0"))
# Document-embedding cache (fastembed only, ingest only, keyed sha256(text)+model id); "" disables
EMBED_CACHE_PATH = os.getenv("EMBED_CACHE_PATH", str(ROOT / "embed_cache.sqlite"))
# Where fastembed stores downloaded ONNX models ("" = fastembed's default temp dir)
MODEL_CACHE_DIR = os.getenv("MODEL_CACHE_DIR", "")
# UTC-day estimated LLM spend cap; once reached, answers fall back to retrieval-only (0 disables the LLM)
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "5.0"))
# Outbound call timeouts (seconds): Qdrant server mode, Postgres connect, Google Drive HTTP
QDRANT_TIMEOUT_S = int(os.getenv("QDRANT_TIMEOUT_S", "5"))
DB_CONNECT_TIMEOUT_S = int(os.getenv("DB_CONNECT_TIMEOUT_S", "5"))
DRIVE_TIMEOUT_S = float(os.getenv("DRIVE_TIMEOUT_S", "10"))
