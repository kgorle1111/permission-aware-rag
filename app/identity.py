"""Identity propagation: the retrieval API knows the real end-user, verified.

JWTs are RS256-signed by the IdP (scripts/gen_keys.py plays IdP locally; set
JWKS_URL for a real Okta/Auth0/Azure AD). The API holds only the public key —
it can verify tokens but never forge them. Groups come from the token's signed
claims, never from anything the client asserts about itself in the request.

FAIL CLOSED: any verification problem = 401 and no retrieval. There is no
"default user" and no unauthenticated path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import jwt
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
        return "|".join(sorted(self.principals))


_public_key_cache: str | None = None
_jwks_client = None


def _public_key():
    global _public_key_cache, _jwks_client
    if config.JWKS_URL:
        if _jwks_client is None:
            _jwks_client = jwt.PyJWKClient(config.JWKS_URL)
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
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"invalid token: {e}")
    except Exception:
        # key file missing, unreadable, etc. — fail closed, reveal nothing
        raise HTTPException(401, "identity layer unavailable")
    groups = claims.get("groups", [])
    if not isinstance(groups, list):
        raise HTTPException(401, "invalid token: groups claim malformed")
    return Principal(user_id=claims["sub"], groups=tuple(str(g) for g in groups))


def principal_from_request(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return verify_token(auth.removeprefix("Bearer ").strip())
