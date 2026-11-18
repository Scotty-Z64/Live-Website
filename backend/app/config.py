"""Central configuration. Every external integration key from the blueprint (§3) lives here."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), ".env"), extra="ignore", case_sensitive=False
    )

    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    frontend_origin: str = "http://localhost:3000"

    database_url: str = "postgresql+psycopg://tendersync_app:tendersync_app@localhost:5432/tendersync"
    redis_url: str = "redis://localhost:6379/0"
    celery_eager: bool = False

    storage_backend: str = "local"  # "s3" | "local"
    local_storage_dir: str = str(REPO_ROOT / "var" / "storage")
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket: str = "tendersync-sa-vault"
    aws_region: str = "af-south-1"
    aws_kms_key_id: str = ""

    local_master_key: str = ""
    pii_hash_key: str = ""
    jwt_secret: str = "change-me"
    jwt_ttl_minutes: int = 60 * 12

    azure_doc_intel_endpoint: str = ""
    azure_doc_intel_key: str = ""
    ocr_confidence_threshold: float = 0.85

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    clamav_host: str = "localhost"
    clamav_port: int = 3310
    # Dev escape hatch: when clamd is unreachable in development, uploads are accepted
    # but tagged av_status=SKIPPED_DEV. In production an unreachable scanner rejects uploads.
    clamav_required: bool | None = None

    playwright_headless: bool = True
    submission_otp_timeout_s: int = 900

    vat_rate: float = 0.15
    tax_pin_evaluation_buffer_days: int = 30

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def av_required(self) -> bool:
        return self.is_production if self.clamav_required is None else self.clamav_required


@lru_cache
def get_settings() -> Settings:
    return Settings()
