"""Tender persistence: manual pack upload, harvested-pack download and pre-flight application."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import clamav
from ..core.crypto import sha256_bytes
from ..core.storage import get_storage, tenant_key
from ..db import tenant_session
from ..models import (
    AuditEvent,
    BidPackage,
    BriefingGateStatus,
    PreferenceSystem,
    SubmissionMode,
    Tender,
    TenderFile,
)
from . import preflight
from .graph import run_preflight

log = logging.getLogger(__name__)


def store_pack(session: Session, tender: Tender, pack_name: str, pack: bytes, source_url: str | None = None) -> list[preflight.PackFile]:
    """AV-scan, unpack, store every file and record SHA-256 hashes for the hash-watcher."""
    try:
        clamav.scan_bytes(pack)
    except clamav.VirusDetected as exc:
        raise ValueError(f"Tender pack rejected: {exc}") from exc
    files = preflight.unpack(pack_name, pack)
    storage = get_storage()
    hashes = dict(tender.source_files_sha256 or {})
    existing = {tf.sha256_hash for tf in session.scalars(select(TenderFile).where(TenderFile.tender_id == tender.id))}
    for f in files:
        digest = sha256_bytes(f.data)
        hashes[source_url and f"{source_url}#{f.name}" or f.name] = digest
        if digest in existing:
            continue
        key = tenant_key(tender.tenant_id, "tenders", str(tender.id), f"{digest[:12]}-{f.name.replace('/', '_')[-120:]}")
        storage.put(key, f.data)
        session.add(TenderFile(
            tenant_id=tender.tenant_id, tender_id=tender.id, filename=f.name, source_url=source_url, s3_key=key,
            sha256_hash=digest, file_kind=f.kind, page_count=preflight.file_page_count(f.data) if f.name.lower().endswith(".pdf") else None,
            is_addendum=f.is_addendum,
        ))
    tender.source_files_sha256 = hashes
    return files


def apply_preflight(session: Session, tender: Tender, pack_name: str, pack: bytes) -> dict:
    state = run_preflight(str(tender.tenant_id), str(tender.id), pack_name, pack)
    fields = state["fields"]
    if fields.get("tender_reference") and tender.tender_reference.startswith("MANUAL-"):
        tender.tender_reference = fields["tender_reference"][:250]
    if fields.get("closing_timestamp"):
        tender.closing_timestamp = _aware(datetime.fromisoformat(fields["closing_timestamp"]))
    if fields.get("preference_system"):
        tender.preference_system = PreferenceSystem(fields["preference_system"])
    if fields.get("functionality_threshold_pct") is not None:
        tender.functionality_threshold_pct = Decimal(str(fields["functionality_threshold_pct"]))
    tender.submission_mode = SubmissionMode(fields.get("submission_mode", "MANUAL_HARDCOPY"))
    tender.portal_url = fields.get("portal_url") or tender.portal_url
    tender.physical_tender_box_address = fields.get("physical_tender_box_address") or tender.physical_tender_box_address
    b = fields.get("briefing")
    tender.has_compulsory_briefing = bool(b and b.get("compulsory"))
    if b:
        tender.briefing_timestamp = _aware(datetime.fromisoformat(b["timestamp"])) if b.get("timestamp") else None
        tender.briefing_venue_or_link = b.get("venue_or_link")
        tender.briefing_registration_url = b.get("registration_url")
    tender.mandatory_schedules = fields.get("mandatory_schedules", [])
    if state.get("boq"):
        tender.boq_line_items = state["boq"]
    tender.preflight = {
        **preflight.summarise_for_ui(fields, state["zones"]),
        "citations": state.get("citations", {}),
        "llm_used": state.get("llm_used", False),
        "errors": state.get("errors", []),
    }
    for bid in session.scalars(select(BidPackage).where(BidPackage.tender_id == tender.id)):
        bid.preflight_complete = True
        init_gate(bid, tender)
    session.add(AuditEvent(tenant_id=tender.tenant_id, entity="tender", entity_id=str(tender.id), action="PREFLIGHT",
                           detail={"fields": sorted(k for k in fields if k != "file_manifest"), "llm_used": state.get("llm_used")}))
    return tender.preflight


def init_gate(bid: BidPackage, tender: Tender) -> None:
    if not tender.has_compulsory_briefing:
        if bid.briefing_gate_status in (None, BriefingGateStatus.PENDING_REGISTRATION, BriefingGateStatus.NOT_REQUIRED):
            bid.briefing_gate_status = BriefingGateStatus.NOT_REQUIRED
    elif bid.briefing_gate_status in (None, BriefingGateStatus.NOT_REQUIRED):
        bid.briefing_gate_status = BriefingGateStatus.PENDING_REGISTRATION


def _aware(dt: datetime) -> datetime:
    # Tender times in South Africa are SAST (UTC+2) unless stated.
    from datetime import timedelta

    return dt if dt.tzinfo else dt.replace(tzinfo=timezone(timedelta(hours=2)))


def create_manual_tender(tenant_id: str, filename: str, data: bytes) -> str:
    with tenant_session(tenant_id) as session:
        tender = Tender(
            tenant_id=uuid.UUID(tenant_id), source_portal="MANUAL_UPLOAD", source_tier="MANUAL",
            tender_reference=f"MANUAL-{uuid.uuid4().hex[:8].upper()}", title=filename, status="OPEN",
            source_files_sha256={}, mandatory_schedules=[], boq_line_items=[], addenda_history=[], preflight={},
        )
        session.add(tender)
        session.flush()
        store_pack(session, tender, filename, data)
        apply_preflight(session, tender, filename, data)
        return str(tender.id)


def load_pack(session: Session, tender: Tender) -> tuple[str, bytes] | None:
    """Rebuild an in-memory ZIP of all stored (non-addendum and addendum) files for re-analysis."""
    import io
    import zipfile

    files = list(session.scalars(select(TenderFile).where(TenderFile.tender_id == tender.id)))
    if not files:
        return None
    buf = io.BytesIO()
    storage = get_storage()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for tf in files:
            zf.writestr(tf.filename, storage.get(tf.s3_key))
    return f"{tender.tender_reference}.zip", buf.getvalue()
