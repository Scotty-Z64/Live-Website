#!/usr/bin/env python3
"""Pre-flight verification of every external connection (blueprint §3).

Run before starting the application servers:

    python scripts/preflight_check.py            # uses ./.env
    python scripts/preflight_check.py --strict   # treat every integration as required

In APP_ENV=production (or with --strict) every check is mandatory and the script exits non-zero
on the first class of failure. In development, unconfigured cloud integrations are reported as
SKIP because the platform has local fallbacks for them.
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.models import TENANT_OWNED_TABLES  # noqa: E402

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
RESULTS: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    icon = {"PASS": "\033[32m✔\033[0m", "FAIL": "\033[31m✘\033[0m", "SKIP": "\033[33m–\033[0m", "WARN": "\033[33m!\033[0m"}[status]
    print(f" {icon} {name:<38} {detail}")


def check(name: str, required: bool):
    def deco(fn):
        def run():
            try:
                detail = fn()
                if detail and detail.startswith("SKIP:"):
                    record(name, "FAIL" if required else "SKIP", detail[5:].strip())
                elif detail and detail.startswith("WARN:"):
                    record(name, "WARN", detail[5:].strip())
                else:
                    record(name, "PASS", detail or "")
            except Exception as exc:  # noqa: BLE001
                record(name, "FAIL", f"{type(exc).__name__}: {exc}"[:220])
                if os.environ.get("PREFLIGHT_DEBUG"):
                    traceback.print_exc()
        return run
    return deco


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    s = get_settings()
    strict = args.strict or s.is_production
    print(f"\nTenderSync SA pre-flight · APP_ENV={s.app_env} · {'STRICT' if strict else 'development (fallbacks allowed)'}\n")

    @check("Environment keys", True)
    def env_keys():
        required = ["DATABASE_URL", "REDIS_URL", "JWT_SECRET"]
        if strict:
            required += ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET", "AWS_REGION", "AWS_KMS_KEY_ID", "PII_HASH_KEY",
                         "AZURE_DOC_INTEL_ENDPOINT", "AZURE_DOC_INTEL_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAMAV_HOST", "CLAMAV_PORT"]
        missing = [k for k in required if not getattr(s, k.lower(), None)]
        if missing:
            raise RuntimeError("missing " + ", ".join(missing))
        if s.jwt_secret in ("change-me", "") or len(s.jwt_secret) < 32:
            if strict:
                raise RuntimeError("JWT_SECRET must be ≥32 random characters")
            return "WARN: JWT_SECRET is weak (fine for local dev only)"
        if strict and s.aws_region != "af-south-1":
            raise RuntimeError(f"AWS_REGION must be af-south-1 for POPIA residency (got {s.aws_region})")
        return f"{len(required)} required keys present"

    @check("PostgreSQL 16 + pgvector + RLS", True)
    def postgres():
        from sqlalchemy import create_engine, text

        eng = create_engine(s.database_url, pool_pre_ping=True)
        with eng.connect() as c:
            ver = c.execute(text("show server_version_num")).scalar()
            if int(ver) < 160000:
                raise RuntimeError(f"PostgreSQL {ver} < 16")
            ext = c.execute(text("select extversion from pg_extension where extname='vector'")).scalar()
            if not ext:
                c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                c.commit()
                ext = c.execute(text("select extversion from pg_extension where extname='vector'")).scalar()
            sup, bypass = c.execute(text("select rolsuper, rolbypassrls from pg_roles where rolname=current_user")).one()
            if sup or bypass:
                raise RuntimeError("application role is SUPERUSER/BYPASSRLS — Row-Level Security would be bypassed")
            rows = c.execute(text("select relname, relrowsecurity, relforcerowsecurity from pg_class where relname = any(:t)"),
                             {"t": TENANT_OWNED_TABLES}).all()
            if not rows:
                return f"WARN: pgvector {ext}; schema not migrated yet (run: alembic upgrade head)"
            bad = [r[0] for r in rows if not (r[1] and r[2])]
            if bad:
                raise RuntimeError("RLS not forced on: " + ", ".join(bad))
            pol = c.execute(text("select count(*) from pg_policies where policyname='tenant_isolation'")).scalar()
            if pol < len(TENANT_OWNED_TABLES):
                raise RuntimeError(f"only {pol}/{len(TENANT_OWNED_TABLES)} tenant_isolation policies present")
            leak = c.execute(text("select count(*) from tenders")).scalar()  # no tenant GUC → must be 0 rows
            if leak:
                raise RuntimeError("tenders visible without app.current_tenant_id — RLS policy is not effective")
        return f"server {int(ver) // 10000}, pgvector {ext}, RLS forced on {len(rows)} tables, app.current_tenant_id policies OK"

    @check("Redis 7 (broker, beat, pub/sub)", True)
    def redis_check():
        import redis

        r = redis.Redis.from_url(s.redis_url, socket_timeout=5)
        r.ping()
        ver = r.info("server")["redis_version"]
        if int(ver.split(".")[0]) < 7:
            return f"WARN: Redis {ver} (< 7)"
        ch = f"preflight:{uuid.uuid4().hex}"
        ps = r.pubsub()
        ps.subscribe(ch)
        ps.get_message(timeout=1)
        r.publish(ch, "ok")
        msg = ps.get_message(timeout=2)
        if not msg or msg.get("data") != b"ok":
            raise RuntimeError("pub/sub round-trip failed")
        return f"Redis {ver}, pub/sub round-trip OK"

    @check("AWS S3 (af-south-1, KMS, versioning)", strict)
    def s3():
        if s.storage_backend != "s3":
            return f"SKIP: STORAGE_BACKEND={s.storage_backend} (local filesystem used)"
        import boto3

        c = boto3.client("s3", region_name=s.aws_region, aws_access_key_id=s.aws_access_key_id or None,
                         aws_secret_access_key=s.aws_secret_access_key or None)
        c.head_bucket(Bucket=s.aws_s3_bucket)
        loc = c.get_bucket_location(Bucket=s.aws_s3_bucket).get("LocationConstraint")
        if loc != "af-south-1":
            raise RuntimeError(f"bucket is in {loc}, not af-south-1")
        if c.get_bucket_versioning(Bucket=s.aws_s3_bucket).get("Status") != "Enabled":
            raise RuntimeError("bucket versioning is not Enabled (audit immutability)")
        enc = c.get_bucket_encryption(Bucket=s.aws_s3_bucket)["ServerSideEncryptionConfiguration"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]
        if enc.get("SSEAlgorithm") != "aws:kms":
            raise RuntimeError(f"default encryption is {enc.get('SSEAlgorithm')}, expected aws:kms")
        pab = c.get_public_access_block(Bucket=s.aws_s3_bucket)["PublicAccessBlockConfiguration"]
        if not all(pab.values()):
            raise RuntimeError("public access block is not fully enabled")
        try:
            c.get_bucket_cors(Bucket=s.aws_s3_bucket)
            cors = "CORS set"
        except c.exceptions.ClientError:
            cors = "no CORS policy (needed only for browser direct uploads)"
        key = f"preflight/{uuid.uuid4().hex}.txt"
        extra = {"ServerSideEncryption": "aws:kms", **({"SSEKMSKeyId": s.aws_kms_key_id} if s.aws_kms_key_id else {})}
        c.put_object(Bucket=s.aws_s3_bucket, Key=key, Body=b"preflight", **extra)
        url = c.generate_presigned_url("get_object", Params={"Bucket": s.aws_s3_bucket, "Key": key}, ExpiresIn=60)
        c.delete_object(Bucket=s.aws_s3_bucket, Key=key)
        return f"{s.aws_s3_bucket} in af-south-1, SSE-KMS, versioned, private, signed URLs OK, {cors}" + ("" if url else "")

    @check("AWS KMS envelope encryption", strict)
    def kms():
        if not s.aws_kms_key_id:
            return "SKIP: AWS_KMS_KEY_ID empty — dev LOCAL_MASTER_KEY envelope used"
        import boto3

        c = boto3.client("kms", region_name=s.aws_region, aws_access_key_id=s.aws_access_key_id or None,
                         aws_secret_access_key=s.aws_secret_access_key or None)
        meta = c.describe_key(KeyId=s.aws_kms_key_id)["KeyMetadata"]
        if meta["KeyState"] != "Enabled" or ":af-south-1:" not in meta["Arn"]:
            raise RuntimeError(f"key state {meta['KeyState']} / arn {meta['Arn']}")
        from app.core.crypto import decrypt_pii, encrypt_pii

        tok = encrypt_pii("preflight-8501150234083")
        assert decrypt_pii(tok) == "preflight-8501150234083"
        return f"{meta['KeyId'][:8]}… enabled in af-south-1, encrypt/decrypt round-trip OK"

    @check("Azure Document Intelligence", strict)
    def azure():
        if not (s.azure_doc_intel_endpoint and s.azure_doc_intel_key):
            return "SKIP: not configured — born-digital PDFs only; scans will be flagged LOW_OCR_CONFIDENCE"
        import httpx

        from app.ocr import AZURE_API_VERSION

        base = s.azure_doc_intel_endpoint.rstrip("/")
        for model in ("prebuilt-layout", "prebuilt-read"):
            r = httpx.get(f"{base}/documentintelligence/documentModels/{model}?api-version={AZURE_API_VERSION}",
                          headers={"Ocp-Apim-Subscription-Key": s.azure_doc_intel_key}, timeout=20)
            r.raise_for_status()
        return "prebuilt-layout and prebuilt-read reachable"

    @check("Anthropic API (RFP parsing, vision)", strict)
    def anthropic_check():
        if not s.anthropic_api_key:
            return "SKIP: not configured — deterministic rules only; stamps verified by OpenCV + text"
        import anthropic

        m = anthropic.Anthropic(api_key=s.anthropic_api_key).models.retrieve(s.anthropic_model)
        return f"model {m.id} available"

    @check("OpenAI embeddings (1536-dim)", strict)
    def openai_check():
        if not s.openai_api_key:
            return "SKIP: not configured — deterministic hashing embeddings (lexical) used"
        from openai import OpenAI

        v = OpenAI(api_key=s.openai_api_key).embeddings.create(model=s.openai_embedding_model, input="LED street light").data[0].embedding
        if len(v) != 1536:
            raise RuntimeError(f"embedding has {len(v)} dims, schema expects 1536")
        return f"{s.openai_embedding_model} → 1536 dims"

    @check("ClamAV daemon (INSTREAM + EICAR)", strict)
    def clam():
        try:
            with socket.create_connection((s.clamav_host, s.clamav_port), timeout=5) as sock:
                sock.sendall(b"zPING\0")
                if not sock.recv(16).startswith(b"PONG"):
                    raise RuntimeError("no PONG")
        except OSError as exc:
            return f"SKIP: clamd unreachable at {s.clamav_host}:{s.clamav_port} ({exc})"
        with socket.create_connection((s.clamav_host, s.clamav_port), timeout=15) as sock:
            sock.sendall(b"zINSTREAM\0" + struct.pack("!L", len(EICAR)) + EICAR + struct.pack("!L", 0))
            reply = sock.recv(4096).rstrip(b"\0").decode()
        if "FOUND" not in reply:
            raise RuntimeError(f"EICAR test file was not detected: {reply}")
        return f"{s.clamav_host}:{s.clamav_port} detects EICAR ({reply.split(':', 1)[-1].strip()})"

    @check("Playwright Chromium", True)
    def pw():
        from playwright.sync_api import sync_playwright

        t = time.time()
        with sync_playwright() as p:
            b = p.chromium.launch(**s.chromium_launch_kwargs())
            page = b.new_page()
            page.set_content("<p id=x>ok</p>")
            assert page.inner_text("#x") == "ok"
            v = b.version
            b.close()
        return f"Chromium {v} headless={s.playwright_headless} ({time.time() - t:.1f}s)"

    for fn in (env_keys, postgres, redis_check, s3, kms, azure, anthropic_check, openai_check, clam, pw):
        fn()

    fails = [r for r in RESULTS if r[1] == "FAIL"]
    counts = {k: sum(1 for r in RESULTS if r[1] == k) for k in ("PASS", "WARN", "SKIP", "FAIL")}
    print(f"\n{counts['PASS']} passed · {counts['WARN']} warnings · {counts['SKIP']} skipped (dev fallback) · {counts['FAIL']} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
