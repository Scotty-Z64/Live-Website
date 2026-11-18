"""Bid package operations: creation, briefing gate, addenda sign-off, pricing, SBD auto-fill, binder."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ocr
from ..core.crypto import decrypt_pii, sha256_bytes
from ..core.events import bid_channel, publish
from ..core.storage import get_storage, tenant_key
from ..db import system_session
from ..models import (
    AuditEvent,
    BidPackage,
    BriefingGateStatus,
    CatalogItem,
    Director,
    DocCategory,
    Tender,
    TenderFile,
    Tenant,
    ValidationStatus,
    VaultDocument,
)
from ..pdf import binder as binder_mod
from ..pdf import letters, sbd
from ..tenders.service import init_gate
from ..validation import stamp
from ..validation.pipeline import ingest_upload
from . import pricing
from .readiness import LABELS, audit_bid, has_blockers


class GateError(Exception):
    pass


def tenant_dict(tenant_id) -> dict:  # noqa: ANN001
    with system_session() as s:
        t = s.get(Tenant, tenant_id)
        return {c.name: getattr(t, c.name) for c in Tenant.__table__.columns}


def get_or_create_bid(session: Session, tender: Tender) -> BidPackage:
    bid = session.scalar(select(BidPackage).where(BidPackage.tender_id == tender.id))
    if bid:
        return bid
    bid = BidPackage(tenant_id=tender.tenant_id, tender_id=tender.id, preflight_complete=bool(tender.preflight.get("fields")),
                     blocking_issues=[], pricing_summary={}, sbd_outputs={}, submission_receipt={}, briefing_representative={})
    init_gate(bid, tender)
    session.add(bid)
    session.flush()
    session.add(AuditEvent(tenant_id=tender.tenant_id, entity="bid", entity_id=str(bid.id), action="CREATED", detail={}))
    return bid


# ── Compulsory briefing gate (human-in-the-loop) ─────────────────────────────────────────────
def prepare_briefing_pack(session: Session, bid: BidPackage, rep: dict, signatory: dict, actor: str) -> dict:
    tender = session.get(Tender, bid.tender_id)
    if not tender.has_compulsory_briefing:
        raise GateError("This tender has no compulsory briefing.")
    if bid.briefing_gate_status == BriefingGateStatus.VERIFIED_ATTENDED:
        raise GateError("Attendance is already verified.")
    t = tenant_dict(bid.tenant_id)
    when = tender.briefing_timestamp.strftime("%A %d %B %Y, %H:%M") if tender.briefing_timestamp else "As per tender notice"
    pdf = letters.letter_of_authority(
        t, {"tender_reference": tender.tender_reference, "title": tender.title, "issuing_entity": tender.issuing_entity,
            "briefing_when": when, "briefing_venue_or_link": tender.briefing_venue_or_link}, rep, signatory)
    key = tenant_key(bid.tenant_id, "bids", str(bid.id), "letter_of_authority.pdf")
    get_storage().put(key, pdf, "application/pdf")
    bid.briefing_representative = {**rep, "signatory": signatory, "loa_s3_key": key, "prepared_at": datetime.now(timezone.utc).isoformat()}
    # The platform never registers on the bidder's behalf — the user opens the external link.
    bid.briefing_gate_status = BriefingGateStatus.REGISTERED_AWAITING_CERT
    session.add(AuditEvent(tenant_id=bid.tenant_id, actor=actor, entity="bid", entity_id=str(bid.id), action="BRIEFING_PACK_PREPARED",
                           detail={"representative": rep.get("full_name")}))
    audit_bid(session, bid)
    return {"letter_url": get_storage().signed_url(key, filename="Letter_of_Authority.pdf"),
            "registration_url": tender.briefing_registration_url, "venue_or_link": tender.briefing_venue_or_link,
            "briefing_timestamp": tender.briefing_timestamp.isoformat() if tender.briefing_timestamp else None,
            "gate": bid.briefing_gate_status.value}


def upload_attendance_cert(session: Session, bid: BidPackage, filename: str, data: bytes, actor: str) -> dict:
    tender = session.get(Tender, bid.tender_id)
    if not tender.has_compulsory_briefing:
        raise GateError("This tender has no compulsory briefing.")
    doc, _ = ingest_upload(session, bid.tenant_id, filename, data, None)
    doc.is_current = False  # not a vault slot document
    doc.processing_state = "DONE"
    session.flush()
    result = ocr.extract(data)
    text = result.text
    ref_found = bool(tender.tender_reference) and tender.tender_reference.replace(" ", "").lower() in text.replace(" ", "").lower()
    png = ocr.render_page_png(data, 0)
    cands = stamp.detect_candidates(png)
    notes = {
        "kind": "BRIEFING_ATTENDANCE_CERT",
        "tender_reference_found": ref_found,
        "stamp_candidates": len(cands),
        "ocr_confidence": result.confidence,
        "warnings": [w for w, ok in [("Tender reference not found on certificate — confirm it is for this tender.", ref_found or result.confidence < 0.85),
                                      ("No ink stamp detected — ensure the certificate is signed/stamped by the procuring entity.", bool(cands))] if not ok],
    }
    doc.extracted_json = notes
    doc.validation_status = ValidationStatus.VALID
    bid.briefing_cert_doc_id = doc.id
    bid.briefing_gate_status = BriefingGateStatus.VERIFIED_ATTENDED
    session.add(AuditEvent(tenant_id=bid.tenant_id, actor=actor, entity="bid", entity_id=str(bid.id), action="BRIEFING_ATTENDANCE_VERIFIED",
                           detail={"document_id": str(doc.id), **{k: v for k, v in notes.items() if k != "kind"}}))
    session.flush()
    audit_bid(session, bid)
    return {"gate": bid.briefing_gate_status.value, "document_id": str(doc.id), **notes}


# ── Silent addenda sign-off ─────────────────────────────────────────────────────────────────
def acknowledge_addendum(session: Session, bid: BidPackage, addendum_id: str, signer: str) -> dict:
    from ..tenders.service import apply_preflight, load_pack

    tender = session.get(Tender, bid.tender_id)
    history = [dict(a) for a in tender.addenda_history or []]
    target = next((a for a in history if a["id"] == addendum_id), None)
    if not target:
        raise LookupError("addendum not found")
    target.update(acknowledged=True, acknowledged_by=signer, acknowledged_at=datetime.now(timezone.utc).isoformat())
    tender.addenda_history = history
    # Re-run pre-flight on the complete pack (original + addenda) so changed rules take effect.
    pack = load_pack(session, tender)
    if pack:
        apply_preflight(session, tender, *pack)
    session.add(AuditEvent(tenant_id=bid.tenant_id, actor=signer, entity="tender", entity_id=str(tender.id), action="ADDENDUM_SIGNED_OFF",
                           detail={"addendum_id": addendum_id, "sha256": target.get("new_sha256")}))
    session.flush()
    return audit_bid(session, bid)


# ── Pricing ─────────────────────────────────────────────────────────────────────────────────
def auto_price(session: Session, bid: BidPackage, margin_pct: float, overrides: dict | None = None) -> dict:
    tender = session.get(Tender, bid.tender_id)
    overrides = overrides or {}
    matched = pricing.match_boq(session, tender.boq_line_items or [])
    lines = []
    for m in matched:
        ln = str(m["line_no"])
        ov = overrides.get(ln, {})
        match = m.get("match")
        if ov.get("catalog_item_id"):
            ci = session.get(CatalogItem, uuid.UUID(ov["catalog_item_id"]))
            match = {"catalog_item_id": str(ci.id), "sku_code": ci.sku_code, "item_name": ci.item_name,
                     "unit_price_excl_vat": float(ci.unit_price_excl_vat),
                     "local_content_pct": float(ci.local_content_pct) if ci.local_content_pct is not None else None, "similarity": None}
        unit_cost = ov.get("unit_cost")
        if unit_cost is None and match:
            unit_cost = match["unit_price_excl_vat"]
        lines.append({"line_no": ln, "description": m["description"], "unit": m.get("unit"), "quantity": m["quantity"],
                      "match": match, "candidates": m.get("candidates", []), "unit_cost": unit_cost,
                      "local_content_pct": (match or {}).get("local_content_pct"), "manual": "unit_cost" in ov})
    summary = pricing.compute(lines, margin_pct)
    summary["overrides"] = overrides
    bid.pricing_summary = summary
    session.flush()
    audit_bid(session, bid)
    return summary


# ── SBD auto-fill ───────────────────────────────────────────────────────────────────────────
def sbd_values(session: Session, bid: BidPackage) -> dict[str, str | None]:
    tender = session.get(Tender, bid.tender_id)
    t = tenant_dict(bid.tenant_id)
    docs = {d.doc_category: d for d in session.scalars(select(VaultDocument).where(
        VaultDocument.is_current.is_(True), VaultDocument.validation_status == ValidationStatus.VALID))}
    tcs = docs.get(DocCategory.SARS_TCS_PIN)
    p = bid.pricing_summary or {}
    pref = tender.preference_system.value if tender.preference_system else None
    instrument = ("Sworn affidavit" if DocCategory.BBBEE_AFFIDAVIT in docs else
                  "SANAS verification certificate" if DocCategory.BBBEE_SANAS_CERT in docs else None)
    level = t.get("bee_level")
    v: dict[str, str | None] = {
        "bid_number": tender.tender_reference,
        "closing_date": tender.closing_timestamp.astimezone().strftime("%Y-%m-%d") if tender.closing_timestamp else None,
        "closing_time": tender.closing_timestamp.astimezone().strftime("%H:%M") if tender.closing_timestamp else None,
        "bid_description": tender.title,
        "supplier_name": t["legal_name"],
        "trading_name": t.get("trading_name"),
        "registration_number": t.get("cipc_reg_number"),
        "postal_address": t.get("physical_address"),
        "street_address": t.get("physical_address"),
        "telephone": t.get("contact_phone"),
        "email": t.get("contact_email"),
        "contact_person": t.get("contact_person"),
        "vat_number": t.get("vat_number"),
        "csd_number": t.get("csd_maaa_number"),
        "tcs_pin": (tcs.extracted_json.get("tcs_pin") if tcs else None),
        "bbbee_level": f"Level {level}" if level and instrument else None,
        "bbbee_instrument": instrument,
        "total_excl_vat": f"R {p['subtotal_excl_vat']:,.2f}" if p.get("subtotal_excl_vat") is not None and not p.get("unpriced_lines") else None,
        "vat_amount": f"R {p['vat']:,.2f}" if p.get("vat") is not None and not p.get("unpriced_lines") else None,
        "total_bid_price": f"R {p['total_incl_vat']:,.2f}" if p.get("total_incl_vat") is not None and not p.get("unpriced_lines") else None,
        "preference_system": pref.replace("_", "/") if pref else None,
        "points_claimed": str(pricing.preference_points(pref, level)) if pref and level and instrument else None,
        "local_content_pct": f"{p['local_content_weighted_pct']}%" if p.get("local_content_weighted_pct") is not None else None,
    }
    for i, d in enumerate(session.scalars(select(Director).order_by(Director.full_name)), 1):
        if i > 6:
            break
        v[f"director_{i}_name"] = d.full_name
        v[f"director_{i}_id"] = decrypt_pii(d.sa_id_encrypted, "sa_id")
        v[f"director_{i}_share"] = f"{d.shareholding_pct}%" if d.shareholding_pct is not None else None
    return v


def autofill_sbds(session: Session, bid: BidPackage, actor: str) -> dict:
    tender = session.get(Tender, bid.tender_id)
    values = sbd_values(session, bid)
    storage = get_storage()
    supplied: dict[str, TenderFile] = {}
    for tf in session.scalars(select(TenderFile).where(TenderFile.tender_id == tender.id, TenderFile.file_kind.in_(["SBD", "ANNEXURE", "RFP"]))):
        if not tf.filename.lower().endswith(".pdf"):
            continue
        data = storage.get(tf.s3_key)
        try:
            pages, n = ocr.text_layer(data)
        except Exception:  # noqa: BLE001
            continue
        if n <= 6 and (form := sbd.identify_form(pages[0] if pages else "")) and form not in supplied:
            supplied[form] = tf
    outputs = {}
    p = bid.pricing_summary or {}
    for form in sbd.FORMS:
        if form in supplied:
            source, src_name = storage.get(supplied[form].s3_key), supplied[form].filename
        else:
            extra = None
            if form == "SBD 3.1" and p.get("lines"):
                extra = [["ITEM", "DESCRIPTION", "UNIT", "QTY", "RATE", "AMOUNT"]] + [
                    [l["line_no"], l["description"], l.get("unit") or "", f"{l['quantity']:g}",
                     f"{l['unit_price']:,.2f}" if l.get("unit_price") is not None else "—",
                     f"{l['line_total']:,.2f}" if l.get("line_total") is not None else "—"] for l in p["lines"]]
            source, src_name = sbd.generate_template(form, True, extra), "TenderSync template"
        res = sbd.fill(source, values)
        key = tenant_key(bid.tenant_id, "bids", str(bid.id), f"{form.replace(' ', '_')}.pdf")
        storage.put(key, res.pdf, "application/pdf")
        outputs[form] = {"s3_key": key, "source": src_name, "method": res.method, "filled": res.filled,
                         "missing_values": res.missing_values, "unmatched_widgets": res.unmatched_widgets,
                         "sha256": sha256_bytes(res.pdf)}
    bid.sbd_outputs = outputs
    session.add(AuditEvent(tenant_id=bid.tenant_id, actor=actor, entity="bid", entity_id=str(bid.id), action="SBD_AUTOFILL",
                           detail={f: {"method": o["method"], "filled": len(o["filled"])} for f, o in outputs.items()}))
    session.flush()
    publish(bid_channel(bid.tenant_id, bid.id), "sbd.filled", {f: {k: o[k] for k in ("method", "filled", "missing_values")} for f, o in outputs.items()})
    return outputs


# ── Binder ──────────────────────────────────────────────────────────────────────────────────
VAULT_ORDER = [DocCategory.CIPC_COR14_3, DocCategory.CSD_REPORT, DocCategory.SARS_TCS_PIN, DocCategory.BBBEE_SANAS_CERT,
               DocCategory.BBBEE_AFFIDAVIT, DocCategory.COIDA_LOGS, DocCategory.CIDB_CERT, DocCategory.PSIRA_CERT,
               DocCategory.WIREMAN_LICENSE, DocCategory.BANK_CONFIRMATION, DocCategory.DIRECTOR_ID]


class ExportLocked(Exception):
    def __init__(self, blockers: list[dict]):
        super().__init__("Export locked by blocking issues")
        self.blockers = blockers


def build_sections(session: Session, bid: BidPackage) -> list[binder_mod.Section]:
    storage = get_storage()
    tender = session.get(Tender, bid.tender_id)
    if not bid.sbd_outputs:
        autofill_sbds(session, bid, "system")
    secs = [binder_mod.Section("Standard Bidding Documents", [
        binder_mod.Item(form, storage.get(o["s3_key"]), needs_signature=True) for form, o in bid.sbd_outputs.items()])]
    p = bid.pricing_summary or {}
    if p.get("lines"):
        rows = "".join(f"<tr><td>{l['line_no']}</td><td>{l['description']}</td><td>{l.get('unit') or ''}</td><td style='text-align:right'>{l['quantity']:g}</td>"
                       f"<td style='text-align:right'>{(l.get('unit_price') or 0):,.2f}</td><td style='text-align:right'>{(l.get('line_total') or 0):,.2f}</td></tr>" for l in p["lines"])
        html = (f"<h2>Priced Bill of Quantities — {tender.tender_reference}</h2><table class='kv' style='font-size:8.5pt'>"
                "<tr><td>Item</td><td>Description</td><td>Unit</td><td>Qty</td><td>Rate (R)</td><td>Amount (R)</td></tr>" + rows +
                f"<tr><td></td><td><b>Sub-total excl. VAT</b></td><td></td><td></td><td></td><td style='text-align:right'><b>{p['subtotal_excl_vat']:,.2f}</b></td></tr>"
                f"<tr><td></td><td>VAT {int(p['vat_rate'] * 100)}%</td><td></td><td></td><td></td><td style='text-align:right'>{p['vat']:,.2f}</td></tr>"
                f"<tr><td></td><td><b>TOTAL incl. VAT</b></td><td></td><td></td><td></td><td style='text-align:right'><b>{p['total_incl_vat']:,.2f}</b></td></tr></table>"
                "<div class='sig'><div>Signature of bidder</div><div>Date</div></div>")
        secs.append(binder_mod.Section("Pricing Schedule & BoQ", [binder_mod.Item("Priced Bill of Quantities", letters.html_pdf(html), True)]))
    docs = list(session.scalars(select(VaultDocument).where(VaultDocument.is_current.is_(True), VaultDocument.doc_category.is_not(None))))
    profile = [d for d in docs if d.doc_category == DocCategory.COMPANY_PROFILE]
    if profile:
        secs.append(binder_mod.Section("Company Profile & Methodology", [binder_mod.Item("Company Profile", storage.get(profile[-1].s3_key))]))
    certs = []
    for cat in VAULT_ORDER:
        for d in sorted([d for d in docs if d.doc_category == cat and d.validation_status == ValidationStatus.VALID], key=lambda d: d.uploaded_at):
            title = LABELS[cat] + (f" — {d.extracted_json.get('full_name')}" if cat == DocCategory.DIRECTOR_ID and d.extracted_json.get("full_name") else "")
            certs.append(binder_mod.Item(title, storage.get(d.s3_key), needs_signature=cat == DocCategory.BBBEE_AFFIDAVIT))
    secs.append(binder_mod.Section("Statutory Compliance Certificates", certs))
    if bid.briefing_cert_doc_id:
        cert = session.get(VaultDocument, bid.briefing_cert_doc_id)
        secs.append(binder_mod.Section("Compulsory Briefing Attendance", [binder_mod.Item("Certificate of Attendance", storage.get(cert.s3_key))]))
    acked = [a for a in tender.addenda_history or [] if a.get("acknowledged")]
    if acked:
        t = tenant_dict(bid.tenant_id)
        secs.append(binder_mod.Section("Addenda Acknowledgement", [binder_mod.Item(
            "Addenda sign-off", letters.addendum_acknowledgement(t, {"tender_reference": tender.tender_reference}, acked), True)]))
    return secs


def compile_hardcopy(session: Session, bid: BidPackage, actor: str) -> dict:
    audit_bid(session, bid)
    blockers = has_blockers(bid)
    if blockers:
        raise ExportLocked(blockers)
    tender = session.get(Tender, bid.tender_id)
    t = tenant_dict(bid.tenant_id)
    ref_slug = "".join(ch for ch in tender.tender_reference.upper() if ch.isalnum())[:12]
    meta = binder_mod.BinderMeta(
        bidder_name=t["legal_name"], tender_reference=tender.tender_reference, tender_title=tender.title or "",
        issuing_entity=tender.issuing_entity or "", closing=tender.closing_timestamp.strftime("%d %B %Y %H:%M") if tender.closing_timestamp else "",
        bates_prefix=f"TS-{ref_slug}", initial_every_page=bool((tender.preflight or {}).get("fields", {}).get("initial_every_page")),
        tender_box_address=tender.physical_tender_box_address,
    )
    out = binder_mod.compile_binder(meta, build_sections(session, bid))
    storage = get_storage()
    base = tenant_key(bid.tenant_id, "bids", str(bid.id), "binder")
    bid.compiled_pdf_s3_key = storage.put(f"{base}/01_Master_Bid_Submission.pdf", out["master"], "application/pdf")
    bid.binder_tabs_s3_key = storage.put(f"{base}/02_Printable_Tab_Dividers.pdf", out["tabs"], "application/pdf")
    bid.binder_spine_s3_key = storage.put(f"{base}/03_Lever_Arch_Spine_Label.pdf", out["spine"], "application/pdf")
    bid.binder_zip_s3_key = storage.put(f"{base}/Hardcopy_Binder.zip", out["zip"], "application/zip")
    bid.submission_receipt = {**(bid.submission_receipt or {}), "hardcopy": {
        "total_pages": out["total_pages"], "spine": out["spine_spec"], "sections": out["sections"],
        "signing_rows": len(out["signing_rows"]), "zip_sha256": sha256_bytes(out["zip"]), "compiled_at": out["compiled_at"]}}
    session.add(AuditEvent(tenant_id=bid.tenant_id, actor=actor, entity="bid", entity_id=str(bid.id), action="BINDER_COMPILED",
                           detail={"pages": out["total_pages"], "sha256": sha256_bytes(out["zip"])}))
    session.flush()
    return {"zip_url": storage.signed_url(bid.binder_zip_s3_key, filename=f"{ref_slug}_Hardcopy_Binder.zip"),
            "total_pages": out["total_pages"], "spine": out["spine_spec"], "sections": out["sections"]}
