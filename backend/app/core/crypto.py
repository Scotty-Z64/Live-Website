"""Application-level PII protection (POPIA).

* Envelope encryption: every value gets a fresh 256-bit data key (AES-256-GCM). The data key
  is wrapped by AWS KMS (af-south-1) in production, or by LOCAL_MASTER_KEY in development.
  Token format:  ``v1.<provider>.<b64 wrapped_key>.<b64 nonce>.<b64 ciphertext>``
* Deterministic lookup hash: HMAC-SHA256(PII_HASH_KEY, normalised value) so SA ID numbers can
  be de-duplicated and reconciled without decrypting.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import get_settings


class CryptoConfigError(RuntimeError):
    pass


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@lru_cache
def _kms_client():
    import boto3

    s = get_settings()
    return boto3.client(
        "kms",
        region_name=s.aws_region,
        aws_access_key_id=s.aws_access_key_id or None,
        aws_secret_access_key=s.aws_secret_access_key or None,
    )


def _local_master_key() -> bytes:
    s = get_settings()
    if s.local_master_key:
        key = base64.b64decode(s.local_master_key)
        if len(key) != 32:
            raise CryptoConfigError("LOCAL_MASTER_KEY must be 32 bytes (base64)")
        return key
    if s.is_production:
        raise CryptoConfigError("AWS_KMS_KEY_ID (or LOCAL_MASTER_KEY) is required in production")
    # Development-only deterministic key so local data survives restarts. Never used in production.
    return hashlib.sha256(b"tendersync-dev-master-key:" + s.jwt_secret.encode()).digest()


def _wrap(data_key: bytes) -> tuple[str, bytes]:
    s = get_settings()
    if s.aws_kms_key_id:
        resp = _kms_client().encrypt(KeyId=s.aws_kms_key_id, Plaintext=data_key)
        return "kms", resp["CiphertextBlob"]
    nonce = os.urandom(12)
    return "local", nonce + AESGCM(_local_master_key()).encrypt(nonce, data_key, b"tendersync-dek")


def _unwrap(provider: str, wrapped: bytes) -> bytes:
    if provider == "kms":
        return _kms_client().decrypt(CiphertextBlob=wrapped)["Plaintext"]
    if provider == "local":
        return AESGCM(_local_master_key()).decrypt(wrapped[:12], wrapped[12:], b"tendersync-dek")
    raise CryptoConfigError(f"unknown key provider {provider}")


def encrypt_pii(plaintext: str, context: str = "pii") -> str:
    data_key = AESGCM.generate_key(bit_length=256)
    provider, wrapped = _wrap(data_key)
    nonce = os.urandom(12)
    ct = AESGCM(data_key).encrypt(nonce, plaintext.encode(), context.encode())
    return ".".join(["v1", provider, _b64e(wrapped), _b64e(nonce), _b64e(ct)])


def decrypt_pii(token: str, context: str = "pii") -> str:
    version, provider, wrapped, nonce, ct = token.split(".")
    if version != "v1":
        raise CryptoConfigError(f"unsupported token version {version}")
    data_key = _unwrap(provider, _b64d(wrapped))
    return AESGCM(data_key).decrypt(_b64d(nonce), _b64d(ct), context.encode()).decode()


def _hash_key() -> bytes:
    s = get_settings()
    if s.pii_hash_key:
        return base64.b64decode(s.pii_hash_key)
    if s.is_production:
        raise CryptoConfigError("PII_HASH_KEY is required in production")
    return hashlib.sha256(b"tendersync-dev-hash-key:" + s.jwt_secret.encode()).digest()


def pii_hash(value: str) -> str:
    normalised = "".join(ch for ch in value if ch.isalnum()).upper()
    return hmac.new(_hash_key(), normalised.encode(), hashlib.sha256).hexdigest()


def mask_id(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"{digits[:6]}*****{digits[-2:]}" if len(digits) >= 8 else "*" * len(digits)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
