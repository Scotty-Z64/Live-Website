from __future__ import annotations

from ..core.storage import get_storage
from ..models import BidPackage, Tender, VaultDocument


def iso(dt):  # noqa: ANN001, ANN201
    return dt.isoformat() if dt else None


def vault_doc(d: VaultDocument, with_url: bool = True) -> dict:
    ex = dict(d.extracted_json or {})
    ex.pop("account_number_encrypted", None)
    return {
        "id": str(d.id), "doc_category": d.doc_category.value if d.doc_category else None,
        "original_filename": d.original_filename, "content_type": d.content_type, "sha256_hash": d.sha256_hash,
        "ocr_confidence_score": float(d.ocr_confidence_score) if d.ocr_confidence_score is not None else None,
        "extracted_json": ex, "issue_date": iso(d.issue_date), "expiry_date": iso(d.expiry_date),
        "validation_status": d.validation_status.value if d.validation_status else None,
        "validation_errors": d.validation_errors or [], "processing_state": d.processing_state, "av_status": d.av_status,
        "is_current": d.is_current, "uploaded_at": iso(d.uploaded_at),
        "url": get_storage().signed_url(d.s3_key, filename=d.original_filename) if with_url else None,
    }


def tender(t: Tender, brief: bool = False) -> dict:
    out = {
        "id": str(t.id), "source_portal": t.source_portal, "source_tier": t.source_tier, "source_url": t.source_url,
        "tender_reference": t.tender_reference, "title": t.title, "issuing_entity": t.issuing_entity,
        "sector_category": t.sector_category, "province": t.province, "closing_timestamp": iso(t.closing_timestamp),
        "submission_mode": t.submission_mode.value if t.submission_mode else None,
        "has_compulsory_briefing": t.has_compulsory_briefing, "briefing_timestamp": iso(t.briefing_timestamp),
        "status": t.status, "harvested_at": iso(t.harvested_at),
        "preflight_done": bool((t.preflight or {}).get("fields")),
        "unacknowledged_addenda": sum(1 for a in t.addenda_history or [] if not a.get("acknowledged")),
    }
    if brief:
        return out
    out.update({
        "description": t.description, "portal_url": t.portal_url, "physical_tender_box_address": t.physical_tender_box_address,
        "preference_system": t.preference_system.value if t.preference_system else None,
        "functionality_threshold_pct": float(t.functionality_threshold_pct) if t.functionality_threshold_pct is not None else None,
        "briefing_venue_or_link": t.briefing_venue_or_link, "briefing_registration_url": t.briefing_registration_url,
        "mandatory_schedules": t.mandatory_schedules, "boq_line_items": t.boq_line_items, "addenda_history": t.addenda_history,
        "preflight": t.preflight, "source_files": len([k for k in (t.source_files_sha256 or {})]),
    })
    return out


def bid(b: BidPackage) -> dict:
    storage = get_storage()
    sbd = {f: {**{k: v for k, v in o.items() if k != "s3_key"}, "url": storage.signed_url(o["s3_key"], filename=f"{f.replace(' ', '_')}.pdf")}
           for f, o in (b.sbd_outputs or {}).items()}
    return {
        "id": str(b.id), "tender_id": str(b.tender_id), "preflight_complete": b.preflight_complete,
        "briefing_gate_status": b.briefing_gate_status.value, "briefing_cert_doc_id": str(b.briefing_cert_doc_id) if b.briefing_cert_doc_id else None,
        "briefing_representative": {k: v for k, v in (b.briefing_representative or {}).items() if k != "loa_s3_key"},
        "letter_of_authority_url": storage.signed_url(b.briefing_representative["loa_s3_key"], filename="Letter_of_Authority.pdf")
        if (b.briefing_representative or {}).get("loa_s3_key") else None,
        "readiness_score_pct": float(b.readiness_score_pct) if b.readiness_score_pct is not None else None,
        "blocking_issues": b.blocking_issues or [], "pricing_summary": b.pricing_summary or {}, "sbd_outputs": sbd,
        "binder_zip_url": storage.signed_url(b.binder_zip_s3_key, filename="Hardcopy_Binder.zip") if b.binder_zip_s3_key else None,
        "submission_receipt": b.submission_receipt or {}, "status": b.status, "updated_at": iso(b.updated_at),
    }
