"""Generate the local IdP's RS256 keypair (config/idp_private.pem + idp_public.pem).

The API only ever loads the PUBLIC key — it can verify tokens, never forge
them. In production you delete the private key from this box entirely and set
JWKS_URL to your real IdP (Okta/Auth0/Azure AD).
"""
import sys
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

if __name__ == "__main__":
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = Path(config.JWT_PRIVATE_KEY_PATH)
    pub = Path(config.JWT_PUBLIC_KEY_PATH)
    if priv.exists() or pub.exists():
        raise SystemExit("Key files already exist; refusing to overwrite. Choose new key paths to rotate.")
    priv.parent.mkdir(parents=True, exist_ok=True)
    pub.parent.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    priv.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    pub.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    print(f"wrote {priv}\nwrote {pub}")
