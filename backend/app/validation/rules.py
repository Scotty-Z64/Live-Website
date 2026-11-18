"""Statutory document validators (blueprint §7). Pure functions — no I/O except the optional
stamp verifier callback — so every rule is unit-testable."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..models import DocCategory, ValidationStatus
from .parsing import (
    all_dates,
    find_sa_ids,
    labelled_date,
    labelled_value,
    parse_money,
    parse_pct,
    valid_sa_id,
)

CIPC_REG_RE = re.compile(r"^\d{4}/\d{6}/\d{2}$")
CIPC_REG_SEARCH = re.compile(r"\b(\d{4}/\d{6}/\d{2})\b")
CSD_RE = re.compile(r"^MAAA\d{7}$")
CSD_SEARCH = re.compile(r"\b(MAAA\d{7})\b")
TCS_PIN_SEARCH = re.compile(r"\bPIN\b\s*[:\-]?\s*([A-Z0-9]{10})\b", re.I)
CIDB_GRADE_RE = re.compile(r"\b([1-9])\s?(CE|GB|ME|EB|EP|SB|SC|SD|SE|SF|SG|SH|SI|SJ|SK|SL|SM|SN|SO|SQ)\b(?:\s?(PE))?")

EME_MAX = Decimal("10000000")
QSE_MAX = Decimal("50000000")

REMEDIATION = {
    "LOW_OCR_CONFIDENCE": "The scan is too unclear to read reliably. Re-scan at 300 DPI in colour (flat, no shadows) and upload again.",
    "INVALID_ENTERPRISE_STATUS": "Download a current CIPC enterprise disclosure (CoR14.3 / CoR15.3) from bizportal.gov.za showing status 'In Business'.",
    "CIPC_REG_FORMAT": "Upload the CIPC registration certificate showing the enterprise number in the form YYYY/NNNNNN/NN.",
    "CSD_UNVERIFIED": "Log in to secure.csd.gov.za → 'My Supplier' → resolve Tax Compliance / Bank Account verification, then download a fresh CSD Summary Report (Full).",
    "TAX_PIN_EXPIRED": "Request a new Tax Compliance Status PIN on SARS eFiling (Tax Status → Request TCS → Tender) and upload the new PIN letter.",
    "MISSING_COMMISSIONER_STAMP": "Sign the affidavit in front of a Commissioner of Oaths (SAPS station, attorney or bank). Download the pre-filled affidavit template below.",
    "INVALID_TURNOVER_TIER": "Your turnover tier requires a different B-BBEE instrument. EMEs (≤R10M) and ≥51% Black-owned QSEs (≤R50M) may use a sworn affidavit; all others need a SANAS-accredited verification certificate.",
    "BBBEE_LEVEL_MISMATCH": "The claimed B-BBEE level does not follow from the stated ownership. Correct the affidavit and re-commission it.",
    "DOCUMENT_EXPIRED": "This document has expired. Obtain a current copy from the issuing authority and upload it.",
    "COIDA_EXPIRED": "Request a new Letter of Good Standing on the Department of Employment & Labour CompEasy portal and upload it.",
    "INSUFFICIENT_CIDB_GRADE": "The tender requires a higher CIDB grade/class than your registration. Consider a joint venture or upgrade on the CIDB i-Tender portal.",
    "MISSING_SECTOR_LICENSE": "This tender's scope requires a sector licence (CIDB / PSIRA / Wireman's Licence). Upload the licence for the responsible person.",
    "INVALID_SA_ID": "The South African ID number failed the checksum. Upload a clearer certified copy of the ID.",
    "FIELD_NOT_FOUND": "A required field could not be located. Upload the official full version of this document.",
    "BANK_HOLDER_MISMATCH": "The account holder on the bank confirmation letter must match the registered company name.",
    "SANAS_NOT_FOUND": "Upload a B-BBEE certificate issued by a SANAS-accredited verification agency.",
}


@dataclass
class Flag:
    code: str
    message: str
    severity: str = "BLOCKER"  # BLOCKER | WARNING
    field: str | None = None

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "field": self.field,
            "remediation": REMEDIATION.get(self.code, ""),
        }


@dataclass
class Outcome:
    status: ValidationStatus
    flags: list[Flag] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)
    issue_date: date | None = None
    expiry_date: date | None = None
    directors: list[dict] = field(default_factory=list)  # [{full_name, sa_id, shareholding_pct}]


@dataclass
class TenantFacts:
    legal_name: str = ""
    cipc_reg_number: str | None = None
    csd_maaa_number: str | None = None
    annual_turnover_zar: Decimal | None = None
    black_ownership_pct: Decimal | None = None


StampFn = Callable[[], "object"]  # returns validation.stamp.StampVerdict


# ── Classification for "Bulk Drop Documents" ───────────────────────────────────────────────
CLASSIFIER: list[tuple[DocCategory, list[str]]] = [
    (DocCategory.CIPC_COR14_3, [r"cor\s?14\.3", r"cor\s?15\.3", r"companies and intellectual property commission", r"enterprise status", r"disclosure certificate"]),
    (DocCategory.CSD_REPORT, [r"central supplier database", r"\bcsd\b", r"MAAA\d{7}", r"supplier number"]),
    (DocCategory.SARS_TCS_PIN, [r"tax compliance status", r"\btcs\b", r"south african revenue service", r"\bpin\b"]),
    (DocCategory.BBBEE_SANAS_CERT, [r"\bsanas\b", r"verification agency", r"b-?bbee (?:verification )?certificate"]),
    (DocCategory.BBBEE_AFFIDAVIT, [r"sworn affidavit", r"commissioner of oaths", r"b-?bbee", r"exempted micro enterprise|qualifying small enterprise"]),
    (DocCategory.COIDA_LOGS, [r"letter of good standing", r"compensation fund", r"\bcoida\b", r"compensation for occupational injuries"]),
    (DocCategory.CIDB_CERT, [r"construction industry development board", r"\bcidb\b", r"\bcrs number\b"]),
    (DocCategory.PSIRA_CERT, [r"\bpsira\b", r"private security industry regulatory authority"]),
    (DocCategory.WIREMAN_LICENSE, [r"wireman'?s licen[cs]e", r"installation electrician", r"electrical contractor"]),
    (DocCategory.BANK_CONFIRMATION, [r"bank (?:account )?confirmation", r"account holder", r"branch code"]),
    (DocCategory.DIRECTOR_ID, [r"identity (?:document|card)", r"republic of south africa", r"identity number"]),
    (DocCategory.COMPANY_PROFILE, [r"company profile", r"our services", r"mission", r"vision"]),
]


def classify(text: str) -> DocCategory | None:
    best, best_score = None, 0
    for cat, pats in CLASSIFIER:
        score = sum(1 for p in pats if re.search(p, text, re.I))
        if score > best_score:
            best, best_score = cat, score
    return best if best_score >= 2 else None


# ── helpers ────────────────────────────────────────────────────────────────────────────────
def _expiry_flags(expiry: date | None, today: date, code: str = "DOCUMENT_EXPIRED") -> list[Flag]:
    if expiry and expiry < today:
        return [Flag(code, f"Expired on {expiry.isoformat()}")]
    return []


def extract_director_rows(text: str) -> list[dict]:
    """Lines containing a valid SA ID: name = text before the ID, % = first percentage on the line."""
    rows: list[dict] = []
    seen = set()
    for line in text.splitlines():
        for sa_id in find_sa_ids(line):
            if sa_id in seen:
                continue
            seen.add(sa_id)
            before = line.split(sa_id)[0]
            name = re.sub(r"[^A-Za-z ,'\-]", " ", before)
            name = re.sub(r"\b(director|member|name|id|number|surname|full names?|i)\b", " ", name, flags=re.I)
            name = " ".join(name.replace(",", " ").split())
            pct = parse_pct(line.split(sa_id, 1)[1])
            rows.append({"full_name": name.title(), "sa_id": sa_id, "shareholding_pct": float(pct) if pct is not None else None})
    return rows


def _status(flags: list[Flag], default=ValidationStatus.VALID) -> ValidationStatus:
    codes = {f.code for f in flags if f.severity == "BLOCKER"}
    if not codes:
        return default
    if codes & {"DOCUMENT_EXPIRED", "TAX_PIN_EXPIRED", "COIDA_EXPIRED"}:
        return ValidationStatus.EXPIRED
    if "MISSING_COMMISSIONER_STAMP" in codes:
        return ValidationStatus.MISSING_STAMP
    return ValidationStatus.MISMATCH_ERROR


# ── per-category validators ─────────────────────────────────────────────────────────────────
def validate_cipc(text: str, tenant: TenantFacts, today: date) -> Outcome:
    flags: list[Flag] = []
    reg = (CIPC_REG_SEARCH.search(text) or [None, None])[1]
    status_line = labelled_value(text, r"enterprise status", r"company status", r"status") or ""
    if not reg or not CIPC_REG_RE.match(reg):
        flags.append(Flag("CIPC_REG_FORMAT", "Enterprise number matching ^\\d{4}/\\d{6}/\\d{2}$ not found", field="cipc_reg_number"))
    elif tenant.cipc_reg_number and tenant.cipc_reg_number != reg:
        flags.append(Flag("MISMATCH_ERROR", f"Registration number {reg} differs from profile {tenant.cipc_reg_number}", field="cipc_reg_number"))
    if not re.search(r"\bin business\b", status_line, re.I):
        flags.append(Flag("INVALID_ENTERPRISE_STATUS", f"Enterprise status is '{status_line or 'not found'}', expected 'In Business'", field="enterprise_status"))
    directors = extract_director_rows(text)
    if not directors:
        flags.append(Flag("FIELD_NOT_FOUND", "No active directors with valid SA ID numbers found", field="directors", severity="WARNING"))
    issue = labelled_date(text, r"date of issue", r"issue date", r"date printed", r"date")
    ent_name = labelled_value(text, r"enterprise name", r"company name", r"registered name")
    return Outcome(
        _status(flags),
        flags,
        {"cipc_reg_number": reg, "enterprise_status": status_line, "enterprise_name": ent_name, "director_count": len(directors)},
        issue,
        None,
        directors,
    )


def validate_csd(text: str, tenant: TenantFacts, today: date) -> Outcome:
    flags: list[Flag] = []
    m = CSD_SEARCH.search(text)
    maaa = m.group(1) if m else None
    if not maaa or not CSD_RE.match(maaa):
        flags.append(Flag("CSD_UNVERIFIED", "Supplier number matching ^MAAA\\d{7}$ not found", field="csd_maaa_number"))
    elif tenant.csd_maaa_number and tenant.csd_maaa_number != maaa:
        flags.append(Flag("MISMATCH_ERROR", f"CSD number {maaa} differs from profile {tenant.csd_maaa_number}", field="csd_maaa_number"))
    tax = labelled_value(text, r"tax compliance status", r"tax status") or ""
    bank = labelled_value(text, r"bank account(?: verification)?(?: status)?", r"bank verification") or ""
    if not re.match(r"\s*compliant\b", tax, re.I):
        flags.append(Flag("CSD_UNVERIFIED", f"Tax Compliance Status = '{tax or 'not found'}', expected 'Compliant'", field="tax_status"))
    if not re.match(r"\s*verified\b", bank, re.I):
        flags.append(Flag("CSD_UNVERIFIED", f"Bank Account = '{bank or 'not found'}', expected 'Verified'", field="bank_status"))
    issue = labelled_date(text, r"report date", r"date generated", r"date")
    if issue and (today - issue).days > 90:
        flags.append(Flag("DOCUMENT_EXPIRED", f"CSD report dated {issue} is older than 90 days", severity="WARNING"))
    return Outcome(
        _status(flags), flags,
        {"csd_maaa_number": maaa, "tax_compliance_status": tax, "bank_account_status": bank},
        issue, None, extract_director_rows(text),
    )


def validate_sars(text: str, tenant: TenantFacts, today: date) -> Outcome:
    flags: list[Flag] = []
    m = TCS_PIN_SEARCH.search(text)
    pin = m.group(1).upper() if m else None
    if not pin:
        flags.append(Flag("FIELD_NOT_FOUND", "10-character TCS PIN not found", field="tcs_pin"))
    expiry = labelled_date(text, r"expiry date", r"valid until", r"expires? on", r"pin expiry")
    if not expiry:
        flags.append(Flag("FIELD_NOT_FOUND", "TCS PIN expiry date not found", field="expiry_date"))
    elif expiry < today:
        flags.append(Flag("TAX_PIN_EXPIRED", f"TCS PIN expired on {expiry}"))
    status = labelled_value(text, r"tax compliance status", r"compliance status", r"status") or ""
    if status and re.search(r"non[- ]?compliant", status, re.I):
        flags.append(Flag("TAX_PIN_EXPIRED", f"SARS status is '{status}'"))
    return Outcome(_status(flags), flags, {"tcs_pin": pin, "tcs_status": status}, labelled_date(text, r"date of issue", r"issue date"), expiry)


def bbbee_level_from_ownership(turnover: Decimal | None, black_pct: Decimal | None) -> int | None:
    """Generic Codes (amended 2013): EME/QSE automatic recognition by Black ownership."""
    if turnover is None or black_pct is None or turnover > QSE_MAX:
        return None
    if black_pct >= 100:
        return 1
    if black_pct >= 51:
        return 2
    return 4 if turnover <= EME_MAX else None  # QSE <51% Black-owned must be verified


def validate_bbbee_affidavit(text: str, tenant: TenantFacts, today: date, stamp_fn: StampFn | None) -> Outcome:
    flags: list[Flag] = []
    turnover_line = labelled_value(text, r"annual (?:total )?(?:revenue|turnover)", r"turnover") or ""
    turnover = parse_money(turnover_line) if turnover_line else None
    turnover = turnover if turnover is not None else tenant.annual_turnover_zar
    black = parse_pct(labelled_value(text, r"black ownership", r"(?:percentage )?black[- ]owned", r"owned by black people") or "")
    black = black if black is not None else tenant.black_ownership_pct
    black_women = parse_pct(labelled_value(text, r"black women ownership", r"black female ownership", r"owned by black women") or "")
    level_line = labelled_value(text, r"b-?bbee (?:status )?level", r"level of contributor", r"status level") or ""
    lm = re.search(r"(?:level\s*)?(\d)\b", level_line, re.I)
    claimed_level = int(lm.group(1)) if lm else None

    tier = None
    if turnover is None:
        flags.append(Flag("FIELD_NOT_FOUND", "Annual turnover not stated on affidavit or profile", field="turnover"))
    elif turnover <= EME_MAX:
        tier = "EME"
    elif turnover <= QSE_MAX:
        tier = "QSE"
        if black is None or black < 51:
            flags.append(Flag("INVALID_TURNOVER_TIER", f"QSE (R{turnover:,.0f}) with {black or 0}% Black ownership may not use an affidavit — SANAS certificate required"))
    else:
        tier = "GENERIC"
        flags.append(Flag("INVALID_TURNOVER_TIER", f"Turnover R{turnover:,.0f} exceeds R50M — a SANAS-accredited certificate is required"))

    expected = bbbee_level_from_ownership(turnover, black)
    if claimed_level and expected and claimed_level < expected:
        flags.append(Flag("BBBEE_LEVEL_MISMATCH", f"Claims Level {claimed_level} but {black}% Black ownership supports Level {expected}"))

    stamp_json = None
    commissioned = None
    if stamp_fn is not None:
        verdict = stamp_fn()
        stamp_json = verdict.to_json()
        commissioned = verdict.stamp_date
        if not verdict.stamp_present:
            flags.append(Flag("MISSING_COMMISSIONER_STAMP", verdict.reason or "Commissioner of Oaths stamp not verified"))
        if verdict.signature_present is False:
            flags.append(Flag("MISSING_COMMISSIONER_STAMP", "Deponent/commissioner signature not detected"))
    if commissioned is None:
        dates = [d for d in all_dates(text) if d <= today]
        commissioned = max(dates) if dates else None
    expiry = None
    if commissioned is None:
        flags.append(Flag("FIELD_NOT_FOUND", "Commissioning date not found", field="commissioned_date"))
    else:
        expiry = commissioned + timedelta(days=365)
        flags += _expiry_flags(expiry, today)

    return Outcome(
        _status(flags), flags,
        {
            "tier": tier,
            "turnover_zar": float(turnover) if turnover is not None else None,
            "black_ownership_pct": float(black) if black is not None else None,
            "black_women_ownership_pct": float(black_women) if black_women is not None else None,
            "claimed_level": claimed_level,
            "expected_level": expected,
            "stamp": stamp_json,
        },
        commissioned, expiry, extract_director_rows(text),
    )


def validate_bbbee_sanas(text: str, tenant: TenantFacts, today: date) -> Outcome:
    flags: list[Flag] = []
    if not re.search(r"\bsanas\b", text, re.I):
        flags.append(Flag("SANAS_NOT_FOUND", "No SANAS accreditation reference found on certificate"))
    level_line = labelled_value(text, r"b-?bbee (?:status )?level", r"status level", r"level") or ""
    lm = re.search(r"(\d)\b", level_line)
    expiry = labelled_date(text, r"expiry date", r"valid until", r"date of expiry")
    if not expiry:
        flags.append(Flag("FIELD_NOT_FOUND", "Certificate expiry date not found", field="expiry_date"))
    flags += _expiry_flags(expiry, today)
    black = parse_pct(labelled_value(text, r"black ownership") or "")
    return Outcome(
        _status(flags), flags,
        {"claimed_level": int(lm.group(1)) if lm else None, "agency": labelled_value(text, r"verification agency", r"issued by"),
         "black_ownership_pct": float(black) if black is not None else None},
        labelled_date(text, r"date of issue", r"issue date"), expiry,
    )


def validate_expiring(text: str, today: date, expired_code: str, extra: dict | None = None) -> Outcome:
    flags: list[Flag] = []
    expiry = labelled_date(text, r"expiry date", r"valid until", r"date of expiry", r"expires", r"valid to")
    if not expiry:
        flags.append(Flag("FIELD_NOT_FOUND", "Expiry date not found", field="expiry_date", severity="WARNING"))
    flags += _expiry_flags(expiry, today, expired_code)
    return Outcome(_status(flags), flags, extra or {}, labelled_date(text, r"date of issue", r"issue date"), expiry)


def validate_coida(text: str, tenant: TenantFacts, today: date) -> Outcome:
    nature = labelled_value(text, r"nature of business", r"business description")
    o = validate_expiring(text, today, "COIDA_EXPIRED", {"nature_of_business": nature})
    if not re.search(r"good standing", text, re.I):
        o.flags.append(Flag("FIELD_NOT_FOUND", "Document is not a Letter of Good Standing"))
        o.status = _status(o.flags)
    return o


def parse_cidb_grade(s: str) -> tuple[int, str] | None:
    m = CIDB_GRADE_RE.search(s.upper())
    return (int(m.group(1)), m.group(2)) if m else None


def validate_cidb(text: str, tenant: TenantFacts, today: date) -> Outcome:
    grades = [(int(g), c) for g, c, _ in CIDB_GRADE_RE.findall(text.upper())]
    crs = labelled_value(text, r"crs number", r"registration number")
    o = validate_expiring(text, today, "DOCUMENT_EXPIRED", {"crs_number": crs, "grades": [f"{g}{c}" for g, c in grades]})
    if not grades:
        o.flags.append(Flag("FIELD_NOT_FOUND", "No CIDB grade/class designation found", field="grades"))
        o.status = _status(o.flags)
    return o


def validate_psira(text: str, tenant: TenantFacts, today: date) -> Outcome:
    reg = labelled_value(text, r"psira (?:registration )?(?:number|no\.?)", r"registration (?:number|no\.?)")
    return validate_expiring(text, today, "DOCUMENT_EXPIRED", {"psira_number": reg})


def validate_wireman(text: str, tenant: TenantFacts, today: date) -> Outcome:
    reg = labelled_value(text, r"registration (?:number|no\.?)", r"licen[cs]e (?:number|no\.?)")
    cat = None
    for c in ("Master Installation Electrician", "Installation Electrician", "Single Phase Tester"):
        if re.search(c, text, re.I):
            cat = c
            break
    o = validate_expiring(text, today, "DOCUMENT_EXPIRED", {"registration_number": reg, "category": cat})
    # Wireman's licences are lifetime registrations; missing expiry is not a defect.
    o.flags = [f for f in o.flags if f.field != "expiry_date"]
    o.status = _status(o.flags)
    return o


def validate_bank(text: str, tenant: TenantFacts, today: date) -> Outcome:
    flags: list[Flag] = []
    acc = labelled_value(text, r"account (?:number|no\.?)")
    acc_digits = re.sub(r"\D", "", acc or "")
    holder = labelled_value(text, r"account holder", r"account name")
    branch = labelled_value(text, r"branch code")
    bank = labelled_value(text, r"bank name", r"bank")
    if len(acc_digits) < 7:
        flags.append(Flag("FIELD_NOT_FOUND", "Bank account number not found", field="account_number"))
    if holder and tenant.legal_name:
        a = set(re.sub(r"[^A-Z ]", " ", holder.upper()).split()) - {"PTY", "LTD", "CC", "THE"}
        b = set(re.sub(r"[^A-Z ]", " ", tenant.legal_name.upper()).split()) - {"PTY", "LTD", "CC", "THE"}
        if a and b and len(a & b) / max(len(b), 1) < 0.6:
            flags.append(Flag("BANK_HOLDER_MISMATCH", f"Account holder '{holder}' ≠ '{tenant.legal_name}'"))
    issue = labelled_date(text, r"date", r"issued")
    if issue and (today - issue).days > 90:
        flags.append(Flag("DOCUMENT_EXPIRED", "Bank confirmation letter older than 3 months", severity="WARNING"))
    return Outcome(_status(flags), flags, {"_account_number": acc_digits or None, "account_holder": holder, "branch_code": branch, "bank_name": bank}, issue, None)


def validate_director_id(text: str, tenant: TenantFacts, today: date) -> Outcome:
    flags: list[Flag] = []
    candidates = re.findall(r"(?<!\d)\d{13}(?!\d)", text)
    valid = [c for c in candidates if valid_sa_id(c)]
    if candidates and not valid:
        flags.append(Flag("INVALID_SA_ID", f"ID number {candidates[0][:6]}******* failed SA ID checksum"))
    elif not candidates:
        flags.append(Flag("FIELD_NOT_FOUND", "No 13-digit SA ID number found", field="sa_id"))
    surname = labelled_value(text, r"surname")
    names = labelled_value(text, r"(?:forenames|names|first names)")
    full = " ".join(x for x in [names, surname] if x)
    directors = [{"full_name": full.title(), "sa_id": valid[0], "shareholding_pct": None}] if valid else []
    return Outcome(_status(flags), flags, {"full_name": full.title() or None}, None, None, directors)


def validate(
    category: DocCategory,
    text: str,
    ocr_confidence: float,
    threshold: float,
    tenant: TenantFacts,
    today: date | None = None,
    stamp_fn: StampFn | None = None,
) -> Outcome:
    today = today or date.today()
    if ocr_confidence < threshold:
        f = Flag("LOW_OCR_CONFIDENCE", f"OCR confidence {ocr_confidence:.2f} is below {threshold:.2f}; values were not guessed")
        return Outcome(ValidationStatus.LOW_OCR_CONFIDENCE, [f], {"ocr_confidence": ocr_confidence})
    dispatch = {
        DocCategory.CIPC_COR14_3: validate_cipc,
        DocCategory.CSD_REPORT: validate_csd,
        DocCategory.SARS_TCS_PIN: validate_sars,
        DocCategory.BBBEE_SANAS_CERT: validate_bbbee_sanas,
        DocCategory.COIDA_LOGS: validate_coida,
        DocCategory.CIDB_CERT: validate_cidb,
        DocCategory.PSIRA_CERT: validate_psira,
        DocCategory.WIREMAN_LICENSE: validate_wireman,
        DocCategory.BANK_CONFIRMATION: validate_bank,
        DocCategory.DIRECTOR_ID: validate_director_id,
    }
    if category == DocCategory.BBBEE_AFFIDAVIT:
        return validate_bbbee_affidavit(text, tenant, today, stamp_fn)
    if category == DocCategory.COMPANY_PROFILE:
        return Outcome(ValidationStatus.VALID, [], {"chars": len(text)})
    return dispatch[category](text, tenant, today)
