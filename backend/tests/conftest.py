from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_tmp = tempfile.mkdtemp(prefix="ts-test-")
os.environ.update({
    "APP_ENV": "development",
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://tendersync_app:tendersync_app@localhost:5432/tendersync_test"),
    "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
    "CELERY_EAGER": "true",
    "STORAGE_BACKEND": "local",
    "LOCAL_STORAGE_DIR": str(Path(_tmp) / "storage"),
    "JWT_SECRET": "test-secret-0123456789abcdef0123456789abcdef",
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "AZURE_DOC_INTEL_ENDPOINT": "",
    "CLAMAV_HOST": "127.0.0.1",
    "CLAMAV_PORT": "1",  # nothing listens → dev mode accepts with av_status=SKIPPED_DEV
})
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))


@pytest.fixture(scope="session")
def samples() -> dict[str, Path]:
    import generate_samples

    return generate_samples.write_all(Path(_tmp) / "samples", "http://testserver")


@pytest.fixture(scope="session")
def db():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from app.db import get_engine
    from app.models import TENANT_OWNED_TABLES

    cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    command.upgrade(cfg, "head")
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(TENANT_OWNED_TABLES + ["users", "tenants"]) + " CASCADE"))
    return get_engine()


@pytest.fixture(scope="session")
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
