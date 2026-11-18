"""Compliance audit → blocking issues + readiness score, pushed live to the Action Drawer.

Every issue carries an ``action`` the UI renders inline (Upload Missing Document Now, Prepare
Briefing Pack / Upload Attendance Cert, Review Addendum, Re-run Pre-Flight, Price BoQ)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.events import bid_channel, publish
from ..db import system_session, tenant_session
from ..models import (
    BidPackage,
    BriefingGateStatus,
    Director,
    DocCategory,
    Tender,
    Tenant,
    ValidationStatus,
    VaultDocument,
)
from ..validation.pipeline import reconciliation_issues
from ..validation.rules import EME_MAX, QSE_MAX, REMEDIATION, parse_cidb_grade

LABELS = {
    DocCategory.CIPC_COR14_3: "CIPC Disclosure (CoR14.3)",
    DocCategory.CSD_REPORT: "CSD Summary Report",
    DocCategory.SARS_TCS_PIN: "SARS Tax Compliance PIN",
    DocCategory.BBBEE_AFFIDAVIT: "B-BBEE Sworn Affidavit",
    DocCategory.BBBEE_SANAS_CERT: "B-BBEE SANAS Certificate",
    DocCategory.COIDA_LOGS: "COIDA Letter of Good Standing",
    DocCategory.CIDB_CERT: "CIDB Registration",
    DocCategory.PSIRA_CERT: "PSIRA Registration",
    DocCategory.WIREMAN_LICENSE: "DoEL Wireman's Licence",
    DocCategory.BANK_CONFIRMATION: "Bank Confirmation Letter",
    DocCategory.DIRECTOR_ID: "Certified Director ID",
    DocCategory.COMPANY_PROFILE: "Company Profile",
}


def _issue(code: str, message: str, action: dict | None = None, severity: str = "BLOCKER", remediation: str | None = None) -> dict:
    return {"id": uuid.uuid4().hex[:10], "code": code, "severity": severity, "message": message,
            "remediation": remediation if remediation is not None else REMEDIATION.get(code, ""), "action": action}


def upload_action(cat: DocCategory) -> dict:
    return {"type": "UPLOAD_DOCUMENT", "doc_category": cat.value, "label": "Upload Missing Document Now"}


def required_categories(tender: Tender, tenant: Tenant) -> tuple[list[DocCategory], dict[str, DocCategory]]:
    req = [DocCategory.CIPC_COR14_3, DocCategory.CSD_REPORT, DocCategory.SARS_TCS_PIN]
    turnover = Decimal(tenant.annual_turnover_zar) if tenant.annual_turnover_zar is not None else None
    black = Decimal(tenant.black_ownership_pct) if tenant.black_ownership_pct is not None else None
    if turnover is not None and (turnover <= EME_MAX or (turnover <= QSE_MAX and black is not None and black >= 51)):
        req.append(DocCategory.BBBEE_AFFIDAVIT)
    else:
        req.append(DocCategory.BBBEE_SANAS_CERT)
    names = " ".join(s.get("name", "") for s in tender.mandatory_schedules or []).lower()
    if "coida" in names:
        req.append(DocCategory.COIDA_LOGS)
    if "bank" in names:
        req.append(DocCategory.BANK_CONFIRMATION)
    if "company profile" in names:
        req.append(DocCategory.COMPANY_PROFILE)
    plugins = {p["plugin"]: p for p in (tender.preflight or {}).get("fields", {}).get("sector_plugins", [])}
    sector = {}
    if "CIDB" in plugins:
        sector["CIDB"] = DocCategory.CIDB_CERT
    if "PSIRA" in plugins:
        sector["PSIRA"] = DocCategory.PSIRA_CERT
    if "WIREMAN" in plugins:
        sector["WIREMAN"] = DocCategory.WIREMAN_LICENSE
    if "CIDB" in plugins and DocCategory.COIDA_LOGS not in req:
        req.append(DocCategory.COIDA_LOGS)  # construction work always requires COIDA good standing
    return req, sector


def audit_bid(session: Session, bid: BidPackage) -> dict:
    s = get_settings()
    tender = session.get(Tender, bid.tender_id)
    with system_session() as ss:
        tenant = ss.get(Tenant, bid.tenant_id)
        ss.expunge(tenant)
    now = datetime.now(timezone.utc)
    closing = tender.closing_timestamp
    issues: list[dict] = []
    checks = 0

    def check(ok: bool, issue: dict | None = None) -> None:
        nonlocal checks
        checks += 1
        if not ok and issue:
            issues.append(issue)

    check(bid.preflight_complete, _issue("PREFLIGHT_INCOMPLETE", "Pre-flight RFP analysis has not run on the full tender pack.",
                                         {"type": "RERUN_PREFLIGHT", "label": "Re-Run Pre-Flight & Compliance Audit"},
                                         remediation="Upload the RFP pack or re-run pre-flight."))
    if closing:
        check(closing > now, _issue("TENDER_CLOSED", f"Tender closed on {closing.isoformat()}", None, remediation="This tender can no longer be submitted."))

    docs = list(session.scalars(select(VaultDocument).where(VaultDocument.is_current.is_(True), VaultDocument.doc_category.is_not(None))))
    by_cat: dict[DocCategory, list[VaultDocument]] = {}
    for d in docs:
        by_cat.setdefault(d.doc_category, []).append(d)

    required, sector = required_categories(tender, tenant)
    for cat in required + list(sector.values()):
        is_sector = cat in sector.values()
        present = by_cat.get(cat, [])
        if not present:
            code = "MISSING_SECTOR_LICENSE" if is_sector else "MISSING_DOCUMENT"
            check(False, _issue(code, f"{LABELS[cat]} is required by this tender but not in your vault.", upload_action(cat),
                                remediation=REMEDIATION.get(code, f"Upload your {LABELS[cat]}.")))
            continue
        doc = sorted(present, key=lambda d: d.uploaded_at)[-1]
        if doc.processing_state in ("QUEUED", "PROCESSING"):
            check(False, _issue("DOCUMENT_PROCESSING", f"{LABELS[cat]} is still being validated.", None, "WARNING", ""))
            continue
        if doc.validation_status != ValidationStatus.VALID:
            first = next((e for e in doc.validation_errors if e.get("severity") == "BLOCKER"), (doc.validation_errors or [{}])[0])
            check(False, _issue(first.get("code", doc.validation_status.value if doc.validation_status else "INVALID"),
                                f"{LABELS[cat]}: {first.get('message', 'failed validation')}",
                                {**upload_action(cat), "label": "Upload Missing Document Now", "document_id": str(doc.id)},
                                remediation=first.get("remediation")))
            continue
        check(True)
        # Expiry must cover the closing date (+30-day evaluation buffer for the SARS PIN).
        if closing and doc.expiry_date:
            buffer = s.tax_pin_evaluation_buffer_days if cat == DocCategory.SARS_TCS_PIN else 0
            needed = (closing + timedelta(days=buffer)).date()
            code = {DocCategory.SARS_TCS_PIN: "TAX_PIN_EXPIRED", DocCategory.COIDA_LOGS: "COIDA_EXPIRED"}.get(cat, "DOCUMENT_EXPIRED")
            check(doc.expiry_date >= needed, _issue(
                code, f"{LABELS[cat]} expires {doc.expiry_date} — must be valid until {needed}"
                      + (" (closing date + 30-day evaluation buffer)" if buffer else " (closing date)"),
                upload_action(cat)))

    # Certified IDs for every director on record.
    directors = list(session.scalars(select(Director)))
    id_hashes = {d.get("sa_id_hash") for doc in by_cat.get(DocCategory.DIRECTOR_ID, [])
                 if doc.validation_status == ValidationStatus.VALID for d in doc.extracted_json.get("directors", [])}
    for d in directors:
        check(d.sa_id_hash in id_hashes, _issue("MISSING_DOCUMENT", f"Certified ID copy for director {d.full_name} is missing.",
                                                upload_action(DocCategory.DIRECTOR_ID),
                                                remediation="Upload a certified copy (not older than 3 months) of this director's ID."))

    # CIDB grade ceiling.
    req_grade = (tender.preflight or {}).get("fields", {}).get("cidb_min_grade")
    if req_grade and by_cat.get(DocCategory.CIDB_CERT):
        held = [parse_cidb_grade(g) for g in by_cat[DocCategory.CIDB_CERT][-1].extracted_json.get("grades", [])]
        ok = any(h and h[1] == req_grade["class"] and h[0] >= req_grade["grade"] for h in held)
        check(ok, _issue("INSUFFICIENT_CIDB_GRADE", f"Tender requires CIDB {req_grade['grade']}{req_grade['class']} or higher; "
                         f"you hold {', '.join(by_cat[DocCategory.CIDB_CERT][-1].extracted_json.get('grades', [])) or 'none'}.",
                         upload_action(DocCategory.CIDB_CERT)))

    for r in reconciliation_issues(session):
        check(False, _issue(r["code"], r["message"], {"type": "OPEN_VAULT", "label": "Review documents"},
                            remediation="Directors and ownership must match across CIPC, CSD and B-BBEE records."))

    # Compulsory briefing gate.
    if tender.has_compulsory_briefing:
        gate = bid.briefing_gate_status
        if gate != BriefingGateStatus.VERIFIED_ATTENDED:
            passed = tender.briefing_timestamp and tender.briefing_timestamp < now
            if gate == BriefingGateStatus.PENDING_REGISTRATION:
                action = {"type": "BRIEFING_PACK", "label": "Prepare Briefing Pack"}
                msg = "Compulsory briefing: register a representative and attend."
            else:
                action = {"type": "UPLOAD_ATTENDANCE_CERT", "label": "Upload Attendance Cert"}
                msg = "Compulsory briefing: upload the signed/stamped Certificate of Attendance to unlock this bid."
            if passed and gate == BriefingGateStatus.PENDING_REGISTRATION:
                msg = "The compulsory briefing date has passed without registration — the bid will be disqualified unless attendance is proven."
            check(False, _issue("BRIEFING_GATE_LOCKED", msg, action,
                                remediation="Bot registration for briefings is prohibited; a person must attend and sign the register."))
        else:
            check(True)

    for a in tender.addenda_history or []:
        if not a.get("acknowledged"):
            check(False, _issue("SILENT_ADDENDUM_DETECTED",
                                f"New/changed tender file detected: {a.get('filename')} ({a.get('detected_at', '')[:16]}). Review and sign off before submission.",
                                {"type": "ACK_ADDENDUM", "addendum_id": a["id"], "label": "Review Addendum"},
                                remediation="Open the addendum diff, confirm the changes and sign off."))

    if tender.boq_line_items:
        p = bid.pricing_summary or {}
        unpriced = p.get("unpriced_lines") if p else [l.get("line_no") for l in tender.boq_line_items]
        check(not unpriced, _issue("BOQ_UNPRICED", f"{len(unpriced or [])} BoQ line(s) have no price.",
                                   {"type": "OPEN_PRICING", "label": "Open Pricing Grid"},
                                   remediation="Match each BoQ line to a catalog item or enter a unit cost."))

    blockers = [i for i in issues if i["severity"] == "BLOCKER"]
    score = round(100 * (checks - len(blockers)) / checks, 2) if checks else 0
    bid.blocking_issues = issues
    bid.readiness_score_pct = Decimal(str(score))
    if bid.status not in ("SUBMITTED", "SUBMITTING", "AWAITING_OTP"):
        bid.status = "READY" if not blockers else "BLOCKED"
    session.flush()
    payload = {"bid_id": str(bid.id), "readiness_score_pct": score, "blocking_issues": issues,
               "briefing_gate_status": bid.briefing_gate_status.value, "status": bid.status}
    publish(bid_channel(bid.tenant_id, bid.id), "bid.blockers", payload)
    return payload


def has_blockers(bid: BidPackage, allow: set[str] | None = None) -> list[dict]:
    allow = allow or set()
    return [i for i in bid.blocking_issues or [] if i["severity"] == "BLOCKER" and i["code"] not in allow]


def reaudit_open_bids(tenant_id: str) -> None:
    with tenant_session(tenant_id) as session:
        for bid in session.scalars(select(BidPackage).where(BidPackage.status.notin_(["SUBMITTED", "WITHDRAWN"]))):
            audit_bid(session, bid)
