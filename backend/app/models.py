"""SQLAlchemy models — blueprint §4 plus the supporting auth/audit tables.

Tables carrying ``tenant_id`` are protected by FORCE ROW LEVEL SECURITY (see migration 0001).
``tenants`` and ``users`` are the root/auth registry and are accessed only through explicit
filters in the API layer.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class DocCategory(str, enum.Enum):
    CIPC_COR14_3 = "CIPC_COR14_3"
    CSD_REPORT = "CSD_REPORT"
    SARS_TCS_PIN = "SARS_TCS_PIN"
    BBBEE_AFFIDAVIT = "BBBEE_AFFIDAVIT"
    BBBEE_SANAS_CERT = "BBBEE_SANAS_CERT"
    COIDA_LOGS = "COIDA_LOGS"
    CIDB_CERT = "CIDB_CERT"
    PSIRA_CERT = "PSIRA_CERT"
    WIREMAN_LICENSE = "WIREMAN_LICENSE"
    BANK_CONFIRMATION = "BANK_CONFIRMATION"
    DIRECTOR_ID = "DIRECTOR_ID"
    COMPANY_PROFILE = "COMPANY_PROFILE"


class ValidationStatus(str, enum.Enum):
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    MISSING_STAMP = "MISSING_STAMP"
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    MISMATCH_ERROR = "MISMATCH_ERROR"


class SubmissionMode(str, enum.Enum):
    DIGITAL_PORTAL = "DIGITAL_PORTAL"
    MANUAL_HARDCOPY = "MANUAL_HARDCOPY"
    HYBRID = "HYBRID"


class PreferenceSystem(str, enum.Enum):
    P80_20 = "80_20"
    P90_10 = "90_10"


class BriefingGateStatus(str, enum.Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING_REGISTRATION = "PENDING_REGISTRATION"
    REGISTERED_AWAITING_CERT = "REGISTERED_AWAITING_CERT"
    VERIFIED_ATTENDED = "VERIFIED_ATTENDED"


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    legal_name: Mapped[str] = mapped_column(String(255))
    trading_name: Mapped[str | None] = mapped_column(String(255))
    cipc_reg_number: Mapped[str | None] = mapped_column(String(32))
    csd_maaa_number: Mapped[str | None] = mapped_column(String(16))
    vat_number: Mapped[str | None] = mapped_column(String(16))
    annual_turnover_zar: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    bee_level: Mapped[int | None] = mapped_column(Integer)
    black_ownership_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    black_women_ownership_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    subscribed_cpv_codes: Mapped[list] = mapped_column(JSONB, default=list)
    # Supporting profile fields used for SBD slot-filling and letterheads.
    physical_address: Mapped[str | None] = mapped_column(Text)
    contact_person: Mapped[str | None] = mapped_column(String(255))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    sectors: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Director(Base):
    __tablename__ = "directors"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    sa_id_encrypted: Mapped[str] = mapped_column(Text)
    sa_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    shareholding_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    is_historically_disadvantaged: Mapped[bool | None] = mapped_column(Boolean)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("vault_documents.id", ondelete="SET NULL"))
    __table_args__ = (UniqueConstraint("tenant_id", "sa_id_hash", name="uq_director_tenant_id_hash"),)


class VaultDocument(Base):
    __tablename__ = "vault_documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    doc_category: Mapped[DocCategory | None] = mapped_column(_enum(DocCategory, "doc_category"))
    original_filename: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    s3_key: Mapped[str] = mapped_column(String(1024))
    sha256_hash: Mapped[str] = mapped_column(String(64))
    ocr_confidence_score: Mapped[float | None] = mapped_column(Numeric(5, 4))
    extracted_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    issue_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    validation_status: Mapped[ValidationStatus | None] = mapped_column(_enum(ValidationStatus, "validation_status"))
    validation_errors: Mapped[list] = mapped_column(JSONB, default=list)
    processing_state: Mapped[str] = mapped_column(String(32), default="QUEUED")
    av_status: Mapped[str] = mapped_column(String(32), default="CLEAN")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "sha256_hash", name="uq_vault_tenant_sha256"),)


class CatalogItem(Base):
    __tablename__ = "catalog_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    sku_code: Mapped[str] = mapped_column(String(64))
    item_name: Mapped[str] = mapped_column(String(512))
    technical_specs: Mapped[str | None] = mapped_column(Text)
    unit_of_measure: Mapped[str | None] = mapped_column(String(32))
    unit_price_excl_vat: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    local_content_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    embedding = mapped_column(Vector(1536), nullable=True)
    __table_args__ = (UniqueConstraint("tenant_id", "sku_code", name="uq_catalog_tenant_sku"),)


class Tender(Base):
    __tablename__ = "tenders"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    source_portal: Mapped[str] = mapped_column(String(64))
    source_tier: Mapped[str | None] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(Text)
    source_files_sha256: Mapped[dict] = mapped_column(JSONB, default=dict)  # {url_or_name: sha256}
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    tender_reference: Mapped[str] = mapped_column(String(255))
    issuing_entity: Mapped[str | None] = mapped_column(String(512))
    sector_category: Mapped[str | None] = mapped_column(String(128))
    province: Mapped[str | None] = mapped_column(String(64))
    closing_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_mode: Mapped[SubmissionMode | None] = mapped_column(_enum(SubmissionMode, "submission_mode"))
    portal_url: Mapped[str | None] = mapped_column(Text)
    physical_tender_box_address: Mapped[str | None] = mapped_column(Text)
    preference_system: Mapped[PreferenceSystem | None] = mapped_column(_enum(PreferenceSystem, "preference_system"))
    functionality_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    has_compulsory_briefing: Mapped[bool] = mapped_column(Boolean, default=False)
    briefing_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    briefing_venue_or_link: Mapped[str | None] = mapped_column(Text)
    briefing_registration_url: Mapped[str | None] = mapped_column(Text)
    mandatory_schedules: Mapped[list] = mapped_column(JSONB, default=list)
    boq_line_items: Mapped[list] = mapped_column(JSONB, default=list)
    addenda_history: Mapped[list] = mapped_column(JSONB, default=list)
    # Pre-flight analysis output (zones, sector rules, citations).
    preflight: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    harvested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "source_portal", "tender_reference", name="uq_tender_tenant_portal_ref"),)


class TenderFile(Base):
    __tablename__ = "tender_files"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    tender_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(1024))
    source_url: Mapped[str | None] = mapped_column(Text)
    s3_key: Mapped[str] = mapped_column(String(1024))
    sha256_hash: Mapped[str] = mapped_column(String(64))
    file_kind: Mapped[str] = mapped_column(String(32))  # RFP | BOQ | SBD | ADDENDUM | ANNEXURE | OTHER
    page_count: Mapped[int | None] = mapped_column(Integer)
    is_addendum: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BidPackage(Base):
    __tablename__ = "bid_packages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    tender_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    preflight_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    briefing_gate_status: Mapped[BriefingGateStatus] = mapped_column(
        _enum(BriefingGateStatus, "briefing_gate_status"), default=BriefingGateStatus.NOT_REQUIRED
    )
    briefing_cert_doc_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("vault_documents.id", ondelete="SET NULL"))
    briefing_representative: Mapped[dict] = mapped_column(JSONB, default=dict)
    readiness_score_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    blocking_issues: Mapped[list] = mapped_column(JSONB, default=list)
    pricing_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    sbd_outputs: Mapped[dict] = mapped_column(JSONB, default=dict)  # {form: {s3_key, method, filled_fields}}
    compiled_pdf_s3_key: Mapped[str | None] = mapped_column(String(1024))
    binder_spine_s3_key: Mapped[str | None] = mapped_column(String(1024))
    binder_tabs_s3_key: Mapped[str | None] = mapped_column(String(1024))
    binder_zip_s3_key: Mapped[str | None] = mapped_column(String(1024))
    submission_receipt: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "tender_id", name="uq_bid_tenant_tender"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str | None] = mapped_column(String(255))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


TENANT_OWNED_TABLES = [
    "directors",
    "vault_documents",
    "catalog_items",
    "tenders",
    "tender_files",
    "bid_packages",
    "audit_events",
]
