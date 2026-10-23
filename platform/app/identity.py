"""Identity propagation: the retrieval API knows the real end-user, verified.

JWTs are RS256-signed by the IdP (scripts/gen_keys.py plays IdP locally; set
JWKS_URL for a real Okta/Auth0/Azure AD). Verification requires only the public
key. Local setup and demo token minting also use a private key; production can
mount only a trusted public key or use JWKS. Groups come from signed claims,
never from anything the client asserts about itself in the request.

FAIL CLOSED: any verification problem = 401 and no retrieval. There is no
"default user" and no unauthenticated path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request

from . import config


@dataclass(frozen=True)
class Principal:
    user_id: str
    groups: tuple[str, ...] = field(default_factory=tuple)

    @property
    def principals(self) -> list[str]:
        """The identity strings matched against chunk ACLs."""
        return [f"user:{self.user_id}", *[f"group:{g}" for g in self.groups], "*"]

    @property
    def scope_key(self) -> str:
        """Stable key for permission-scoped caching."""
        return json.dumps(sorted(set(self.principals)), separators=(",", ":"))


_public_key_cache: str | None = None
_jwks_client = None
# bounds both the boot fetch and request-time key refreshes; PyJWT's default is 30 s
JWKS_TIMEOUT_S = 5

NO_IDP_MESSAGE = ("no identity provider configured: set JWKS_URL to your IdP's JWKS endpoint, "
                  "or DEMO_MODE=1 for a local demo")


def validate_identity_config() -> None:
    """Fail loud at boot: a garbage key or an unreachable JWKS must stop startup, not 401 every
    request. Raises RuntimeError naming the env var and the fix; on success the verified key
    (or JWKS client) is cached for verify_token."""
    global _public_key_cache, _jwks_client
    if config.JWKS_URL:
        if config.DEMO_MODE:
            raise RuntimeError(
                "DEMO_MODE=1 cannot be combined with JWKS_URL: demo mode mints its own tokens. "
                "Set DEMO_MODE=0 for a real IdP, or unset JWKS_URL for a local demo")
        client = jwt.PyJWKClient(config.JWKS_URL, timeout=JWKS_TIMEOUT_S)
        try:
            client.get_signing_keys()
        except Exception as e:
            raise RuntimeError(
                f"JWKS_URL={config.JWKS_URL} could not be loaded ({type(e).__name__}: {e}): "
                "set JWKS_URL to your IdP's reachable JWKS endpoint serving an RS256 signing key"
            ) from e
        _jwks_client = client
        return
    path = Path(config.JWT_PUBLIC_KEY_PATH)
    if not path.exists():
        if config.DEMO_MODE:
            raise RuntimeError(f"DEMO_MODE=1 but JWT_PUBLIC_KEY_PATH={path} does not exist: "
                               "run python scripts/gen_keys.py to create a local demo keypair")
        raise RuntimeError(NO_IDP_MESSAGE)
    try:
        pem = path.read_text()
        if not isinstance(serialization.load_pem_public_key(pem.encode()), rsa.RSAPublicKey):
            raise ValueError("not an RSA key")
    except Exception as e:
        raise RuntimeError(
            f"JWT_PUBLIC_KEY_PATH={path} is not a usable RSA public key PEM "
            f"({type(e).__name__}: {e}): mount your IdP's RS256 public key there, "
            "or set JWKS_URL instead") from e
    if config.DEMO_MODE and not Path(config.JWT_PRIVATE_KEY_PATH).is_file():
        raise RuntimeError(f"DEMO_MODE=1 needs the local private key at JWT_PRIVATE_KEY_PATH="
                           f"{config.JWT_PRIVATE_KEY_PATH} to mint demo tokens: run python "
                           "scripts/gen_keys.py with fresh key paths, or set DEMO_MODE=0")
    _public_key_cache = pem


def _public_key():
    global _public_key_cache, _jwks_client
    if config.JWKS_URL:
        if _jwks_client is None:
            _jwks_client = jwt.PyJWKClient(config.JWKS_URL, timeout=JWKS_TIMEOUT_S)
        return _jwks_client
    if _public_key_cache is None:
        _public_key_cache = Path(config.JWT_PUBLIC_KEY_PATH).read_text()
    return _public_key_cache


def verify_token(token: str) -> Principal:
    try:
        key = _public_key()
        if config.JWKS_URL:
            key = key.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"],  # RS256 only — never accept HS256 here
            audience=config.JWT_AUDIENCE,
            issuer=config.JWT_ISSUER or None,
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"invalid token: {e}")
    except Exception:
        # key file missing, unreadable, etc. — fail closed, reveal nothing
        raise HTTPException(401, "identity layer unavailable")
    groups = claims.get("groups", [])
    if (not isinstance(claims["sub"], str) or not claims["sub"].strip()
            or not isinstance(groups, list)
            or any(not isinstance(g, str) or not g.strip() for g in groups)):
        raise HTTPException(401, "invalid token: groups claim malformed")
    return Principal(user_id=claims["sub"], groups=tuple(groups))


def principal_from_request(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return verify_token(auth.removeprefix("Bearer ").strip())
