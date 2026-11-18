"""Vault ingestion: ClamAV → SHA-256 dedup → S3 → OCR → classify → validate → persist."""
from __future__ import annotations

import logging
import mimetypes
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ocr
from ..config import get_settings
from ..core import clamav
from ..core.crypto import encrypt_pii, mask_id, pii_hash, sha256_bytes
from ..core.events import publish, tenant_channel
from ..core.storage import get_storage, tenant_key
from ..db import system_session, tenant_session
from ..models import AuditEvent, Director, DocCategory, Tenant, ValidationStatus, VaultDocument
from . import rules, stamp
from .reconciliation import reconcile

log = logging.getLogger(__name__)
ALLOWED_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/tiff"}
MAX_BYTES = 25 * 1024 * 1024


class UploadRejected(Exception):
    pass


def sniff_type(data: bytes, filename: str) -> str:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def ingest_upload(session: Session, tenant_id: uuid.UUID, filename: str, data: bytes, category: DocCategory | None) -> tuple[VaultDocument, bool]:
    """Synchronous part of an upload. Returns (document, created)."""
    if len(data) > MAX_BYTES:
        raise UploadRejected("File exceeds 25 MB")
    ctype = sniff_type(data, filename)
    if ctype not in ALLOWED_TYPES:
        raise UploadRejected(f"Unsupported file type {ctype}; upload PDF, PNG, JPEG or TIFF")
    try:
        scan = clamav.scan_bytes(data)
    except clamav.VirusDetected as exc:
        raise UploadRejected(str(exc)) from exc
    digest = sha256_bytes(data)
    existing = session.scalar(select(VaultDocument).where(VaultDocument.sha256_hash == digest))
    if existing:
        if category and existing.doc_category != category:
            existing.doc_category = category
            existing.processing_state = "QUEUED"
        return existing, False
    doc_id = uuid.uuid4()
    key = tenant_key(tenant_id, "vault", f"{doc_id}-{digest[:12]}{_ext(ctype)}")
    get_storage().put(key, data, ctype)
    doc = VaultDocument(
        id=doc_id, tenant_id=tenant_id, doc_category=category, original_filename=filename[:500],
        content_type=ctype, s3_key=key, sha256_hash=digest, processing_state="QUEUED",
        av_status=scan.status, extracted_json={}, validation_errors=[],
    )
    session.add(doc)
    session.add(AuditEvent(tenant_id=tenant_id, entity="vault_document", entity_id=str(doc_id), action="UPLOADED",
                           detail={"filename": filename, "sha256": digest, "av": scan.status}))
    return doc, True


def _ext(ctype: str) -> str:
    return {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/tiff": ".tif"}.get(ctype, "")


def tenant_facts(tenant: Tenant) -> rules.TenantFacts:
    return rules.TenantFacts(
        legal_name=tenant.legal_name or "",
        cipc_reg_number=tenant.cipc_reg_number,
        csd_maaa_number=tenant.csd_maaa_number,
        annual_turnover_zar=Decimal(tenant.annual_turnover_zar) if tenant.annual_turnover_zar is not None else None,
        black_ownership_pct=Decimal(tenant.black_ownership_pct) if tenant.black_ownership_pct is not None else None,
    )


def process_document(tenant_id: str, doc_id: str) -> dict:
    settings = get_settings()
    with system_session() as s:
        tenant = s.get(Tenant, uuid.UUID(tenant_id))
        facts = tenant_facts(tenant)
    with tenant_session(tenant_id) as session:
        doc = session.get(VaultDocument, uuid.UUID(doc_id))
        if doc is None:
            raise LookupError(doc_id)
        doc.processing_state = "PROCESSING"
        session.flush()
        data = get_storage().get(doc.s3_key)
        result = ocr.extract(data)
        category = doc.doc_category or rules.classify(result.text)
        if category is None:
            doc.processing_state = "NEEDS_CATEGORY"
            doc.ocr_confidence_score = result.confidence
            doc.validation_status = (
                ValidationStatus.LOW_OCR_CONFIDENCE if result.low_confidence else ValidationStatus.MISMATCH_ERROR
            )
            doc.validation_errors = [{"code": "UNCLASSIFIED", "severity": "BLOCKER",
                                      "message": "Could not determine the document type — choose a vault slot for it.",
                                      "remediation": "Use 'Replace / Upload' on the correct slot."}]
            _publish_doc(tenant_id, doc)
            return {"status": "NEEDS_CATEGORY"}
        doc.doc_category = category

        def stamp_fn():
            page_idx = _page_with(result.pages, r"commissioner of oaths") if result.pages else 0
            png = ocr.render_page_png(data, page_idx, dpi=150)
            return stamp.verify_commissioner_stamp(png, result.pages[page_idx] if result.pages else result.text)

        outcome = rules.validate(category, result.text, result.confidence, settings.ocr_confidence_threshold, facts,
                                 stamp_fn=stamp_fn)
        extracted = dict(outcome.extracted)
        # PII never stored in clear text inside extracted_json.
        if acc := extracted.pop("_account_number", None):
            extracted["account_number_encrypted"] = encrypt_pii(acc, "bank")
            extracted["account_number_masked"] = f"****{acc[-4:]}"
        extracted["ocr_engine"] = result.engine
        extracted["page_count"] = result.page_count
        extracted["directors"] = [
            {"full_name": d["full_name"], "sa_id_masked": mask_id(d["sa_id"]), "sa_id_hash": pii_hash(d["sa_id"]),
             "shareholding_pct": d.get("shareholding_pct")}
            for d in outcome.directors
        ]
        doc.extracted_json = extracted
        doc.ocr_confidence_score = round(result.confidence, 4)
        doc.issue_date = outcome.issue_date
        doc.expiry_date = outcome.expiry_date
        doc.validation_status = outcome.status
        doc.validation_errors = [f.to_json() for f in outcome.flags]
        doc.processing_state = "DONE"

        # Supersede older documents in the same slot (DIRECTOR_ID slots hold one per person).
        if category != DocCategory.DIRECTOR_ID:
            for old in session.scalars(select(VaultDocument).where(
                VaultDocument.doc_category == category, VaultDocument.id != doc.id, VaultDocument.is_current.is_(True)
            )):
                old.is_current = False

        if category in (DocCategory.CIPC_COR14_3, DocCategory.DIRECTOR_ID) and outcome.status != ValidationStatus.LOW_OCR_CONFIDENCE:
            _upsert_directors(session, uuid.UUID(tenant_id), doc, outcome.directors, category)
        session.add(AuditEvent(tenant_id=uuid.UUID(tenant_id), entity="vault_document", entity_id=doc_id, action="VALIDATED",
                               detail={"status": outcome.status.value, "flags": [f.code for f in outcome.flags]}))
        session.flush()
        _publish_doc(tenant_id, doc)

    # Update profile fields that are now verified by a VALID document.
    if outcome.status == ValidationStatus.VALID:
        with system_session() as s:
            t = s.get(Tenant, uuid.UUID(tenant_id))
            if category == DocCategory.CIPC_COR14_3 and not t.cipc_reg_number:
                t.cipc_reg_number = outcome.extracted.get("cipc_reg_number")
            if category == DocCategory.CSD_REPORT and not t.csd_maaa_number:
                t.csd_maaa_number = outcome.extracted.get("csd_maaa_number")
            if category == DocCategory.BBBEE_AFFIDAVIT and outcome.extracted.get("expected_level"):
                t.bee_level = outcome.extracted.get("claimed_level") or outcome.extracted["expected_level"]
                if outcome.extracted.get("black_ownership_pct") is not None:
                    t.black_ownership_pct = Decimal(str(outcome.extracted["black_ownership_pct"]))
                if outcome.extracted.get("black_women_ownership_pct") is not None:
                    t.black_women_ownership_pct = Decimal(str(outcome.extracted["black_women_ownership_pct"]))

    # Any vault change re-audits the tenant's open bids so the Action Drawer updates live.
    from ..bids.readiness import reaudit_open_bids

    reaudit_open_bids(tenant_id)
    return {"status": outcome.status.value, "category": category.value}


def _page_with(pages: list[str], pattern: str) -> int:
    import re

    for i, p in enumerate(pages):
        if re.search(pattern, p, re.I):
            return i
    return 0


def _upsert_directors(session: Session, tenant_id: uuid.UUID, doc: VaultDocument, rows: list[dict], category: DocCategory) -> None:
    for r in rows:
        h = pii_hash(r["sa_id"])
        d = session.scalar(select(Director).where(Director.sa_id_hash == h))
        if d is None:
            d = Director(tenant_id=tenant_id, full_name=r["full_name"] or "Unknown", sa_id_encrypted=encrypt_pii(r["sa_id"], "sa_id"),
                         sa_id_hash=h, source_document_id=doc.id)
            session.add(d)
        if category == DocCategory.CIPC_COR14_3:
            d.full_name = r["full_name"] or d.full_name
            d.source_document_id = doc.id
            if r.get("shareholding_pct") is not None:
                d.shareholding_pct = Decimal(str(r["shareholding_pct"]))


def reconciliation_issues(session: Session) -> list[dict]:
    sources: dict[str, list[dict]] = {}
    for doc in session.scalars(select(VaultDocument).where(VaultDocument.is_current.is_(True))):
        if doc.doc_category and doc.extracted_json.get("directors"):
            sources[doc.doc_category.value] = doc.extracted_json["directors"]
    return reconcile(sources)


def _publish_doc(tenant_id: str, doc: VaultDocument) -> None:
    publish(tenant_channel(tenant_id), "vault.document", {
        "id": str(doc.id), "category": doc.doc_category.value if doc.doc_category else None,
        "status": doc.validation_status.value if doc.validation_status else None,
        "processing_state": doc.processing_state,
    })
