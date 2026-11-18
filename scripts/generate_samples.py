#!/usr/bin/env python3
"""Generate SAMPLE fixtures (fictitious company, people and tender) for tests and E2E verification.

Every page is watermarked "SAMPLE — TEST FIXTURE". These are not real statutory documents.

    python scripts/generate_samples.py [out_dir]
"""
from __future__ import annotations

import io
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import fitz
import openpyxl

TODAY = date.today()
COMPANY = "Lwazi Electrical & Civils (Pty) Ltd"
REG = "2019/123456/07"
CSD = "MAAA1234567"
TENDER_REF = "COJ/EL/2026/045"


def luhn_digit(num12: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(num12)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def sa_id(yymmdd: str, seq: str) -> str:
    base = f"{yymmdd}{seq}08"
    return base + luhn_digit(base)


DIRECTORS = [
    ("Thandiwe Nomsa MOKOENA", sa_id("850115", "0234"), 60.0),
    ("Sipho Johannes DLAMINI", sa_id("790623", "5123"), 40.0),
]


def d(x: date) -> str:
    return x.strftime("%d %B %Y")


def page(doc: fitz.Document, title: str, lines: list[str], issuer: str = "") -> fitz.Page:
    p = doc.new_page(width=595, height=842)
    p.insert_text((50, 60), issuer, fontsize=9, fontname="helv", color=(0.3, 0.3, 0.3))
    p.insert_textbox(fitz.Rect(50, 70, 545, 120), title, fontsize=15, fontname="hebo")
    y = 135
    for line in lines:
        p.insert_text((50, y), line, fontsize=10, fontname="helv")
        y += 17
    p.insert_text((150, 450), "SAMPLE — TEST FIXTURE", fontsize=34, fontname="hebo", color=(0.88, 0.88, 0.88),
                  morph=(fitz.Point(300, 450), fitz.Matrix(1, 0, 0, 1, 0, 0).prerotate(-30)), overlay=False)
    return p


def pdf(title: str, lines: list[str], issuer: str = "", pages: int = 1) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        page(doc, title + (f" (page {i + 1})" if pages > 1 else ""), lines, issuer)
    return doc.tobytes()


def stamp(p: fitz.Page, center: tuple[float, float], text_date: date, color=(0.1, 0.2, 0.75)) -> None:
    cx, cy = center
    p.draw_circle(fitz.Point(cx, cy), 55, color=color, width=3)
    p.draw_circle(fitz.Point(cx, cy), 45, color=color, width=1.5)
    p.insert_text((cx - 38, cy - 12), "COMMISSIONER", fontsize=8, fontname="hebo", color=color)
    p.insert_text((cx - 22, cy), "OF OATHS", fontsize=8, fontname="hebo", color=color)
    p.insert_text((cx - 30, cy + 14), text_date.strftime("%Y-%m-%d"), fontsize=8, fontname="hebo", color=color)
    p.draw_rect(fitz.Rect(cx - 50, cy - 50, cx + 50, cy + 50), color=color, width=0.01, fill=color, fill_opacity=0.08)
    # signature scribble
    pts = [fitz.Point(cx + 70 + i * 6, cy + 30 + (8 if i % 2 else -6)) for i in range(14)]
    p.draw_polyline(pts, color=(0.05, 0.05, 0.4), width=1.6)


def cipc() -> bytes:
    lines = [f"Enterprise Name: {COMPANY}", f"Registration Number: {REG}", "Enterprise Type: Private Company",
             "Enterprise Status: In Business", f"Date of Issue: {d(TODAY - timedelta(days=20))}", "",
             "ACTIVE DIRECTORS / MEMBERS", "Full Names                         ID Number        Shareholding"]
    lines += [f"{n:<34} {i}   {s:.2f}%" for n, i, s in DIRECTORS]
    return pdf("COMPANIES AND INTELLECTUAL PROPERTY COMMISSION — DISCLOSURE CERTIFICATE (CoR14.3)", lines, "CIPC · Republic of South Africa")


def csd(tax="Compliant", bank="Verified") -> bytes:
    lines = [f"Supplier Name: {COMPANY}", f"Supplier Number: {CSD}", f"Registration Number: {REG}",
             f"Tax Compliance Status: {tax}", f"Bank Account Verification: {bank}", f"Report Date: {d(TODAY - timedelta(days=5))}", "",
             "DIRECTORS", *[f"{n:<34} {i}" for n, i, _ in DIRECTORS]]
    return pdf("CENTRAL SUPPLIER DATABASE — SUPPLIER SUMMARY REPORT", lines, "National Treasury · Central Supplier Database")


def sars(expiry: date) -> bytes:
    return pdf("SOUTH AFRICAN REVENUE SERVICE — TAX COMPLIANCE STATUS", [
        f"Taxpayer Name: {COMPANY}", "Tax Compliance Status: Compliant", "Purpose: Tender",
        "PIN: X7K2M9Q4PZ", f"Date of Issue: {d(TODAY - timedelta(days=10))}", f"Expiry Date: {d(expiry)}"],
        "SARS eFiling · TCS")


def affidavit(with_stamp=True, commissioned: date | None = None, turnover="R 4 500 000") -> bytes:
    commissioned = commissioned or TODAY - timedelta(days=30)
    doc = fitz.open()
    p = page(doc, "SWORN AFFIDAVIT — B-BBEE EXEMPTED MICRO ENTERPRISE", [
        f"I, {DIRECTORS[0][0]}, ID {DIRECTORS[0][1]}, declare under oath:",
        f"Enterprise Name: {COMPANY}", f"Registration Number: {REG}",
        f"Annual Turnover: {turnover}", "Black Ownership: 100%", "Black Women Ownership: 60%",
        "B-BBEE Status Level: Level 1", "",
        f"Sworn before me on {d(commissioned)} at Johannesburg.", "Commissioner of Oaths", "Full names: A. Example (SAPS Station Commander)"],
        "Department of Trade, Industry and Competition — Sworn Affidavit")
    if with_stamp:
        stamp(p, (420, 640), commissioned)
    return doc.tobytes()


def coida(expiry: date) -> bytes:
    return pdf("COMPENSATION FUND — LETTER OF GOOD STANDING", [
        f"Employer: {COMPANY}", "Nature of Business: Electrical installation and civil works", "Registration Number: 990012345678",
        "This letter confirms the employer is in good standing with the Compensation Fund (COIDA).",
        f"Date of Issue: {d(TODAY - timedelta(days=15))}", f"Valid Until: {d(expiry)}"], "Department of Employment and Labour")


def cidb(grade="6EP") -> bytes:
    return pdf("CONSTRUCTION INDUSTRY DEVELOPMENT BOARD — REGISTRATION CERTIFICATE", [
        f"Contractor: {COMPANY}", "CRS Number: 10123456", f"Grading Designation: {grade} PE",
        f"Expiry Date: {d(TODAY + timedelta(days=300))}"], "CIDB Register of Contractors")


def wireman() -> bytes:
    return pdf("DEPARTMENT OF EMPLOYMENT AND LABOUR — WIREMAN'S LICENCE", [
        f"Name: {DIRECTORS[1][0]}", f"ID Number: {DIRECTORS[1][1]}", "Category: Master Installation Electrician",
        "Registration Number: MIE-2008-55123", "Electrical contractor registration confirmed."], "Electrical Installation Regulations")


def bank() -> bytes:
    return pdf("BANK CONFIRMATION LETTER", [f"Account Holder: {COMPANY}", "Bank Name: Example Bank Ltd", "Account Number: 6201 2345 678",
                                           "Branch Code: 250655", "Account Type: Current", f"Date: {d(TODAY - timedelta(days=12))}"], "Example Bank")


def director_id(i: int) -> bytes:
    name, sid, _ = DIRECTORS[i]
    surname, *rest = name.split()[::-1]
    return pdf("REPUBLIC OF SOUTH AFRICA — IDENTITY DOCUMENT (CERTIFIED COPY)", [
        f"Surname: {name.split()[-1]}", f"Names: {' '.join(name.split()[:-1])}", f"Identity Number: {sid}",
        "Certified a true copy of the original. Commissioner of Oaths."], "Department of Home Affairs")


def company_profile() -> bytes:
    return pdf("COMPANY PROFILE", [COMPANY, "Our services: LV/MV electrical installations, street lighting, civil works.",
                                   "Mission: safe, compliant and on-time delivery.", "Vision: a trusted Level 1 B-BBEE contractor."],
               "Company Profile", pages=2)


def low_quality_scan() -> bytes:
    """An image-only PDF (no text layer) — exercises the LOW_OCR_CONFIDENCE gate."""
    src = fitz.open(stream=sars(TODAY + timedelta(days=200)), filetype="pdf")
    pix = src[0].get_pixmap(dpi=40)
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.insert_image(p.rect, stream=pix.tobytes("jpeg", jpg_quality=20))
    return doc.tobytes()


def rfp(portal_base: str) -> bytes:
    closing = TODAY + timedelta(days=21)
    briefing = TODAY + timedelta(days=7)
    doc = fitz.open()
    page(doc, "CITY OF JOHANNESBURG — REQUEST FOR PROPOSAL", [
        f"Tender Number: {TENDER_REF}",
        "Description: Supply and installation of LED street lighting, Region F",
        f"Closing Date: {closing.strftime('%d %B %Y')} at 11:00",
        "", "PART T1: TENDERING PROCEDURES",
        "1.1 Compulsory briefing session",
        f"Date: {briefing.strftime('%d %B %Y')}  Time: 10:00",
        "Venue: 66 Jorissen Street, Braamfontein, Johannesburg, Committee Room 3",
        "Register at https://example.org/briefings/coj-el-2026-045 to confirm attendance.",
        "Bidders who fail to attend the compulsory briefing session will be disqualified.",
        "1.2 Evaluation: the 80/20 preference point system applies.",
        "Bids must achieve a minimum of 70% for functionality to proceed to price evaluation.",
        "Tenderers must hold a CIDB grading of 5EP or higher.",
        "The bid document must be initialled on every page by the authorised signatory.",
        "Submission: bids may be deposited in the tender box at Metro Centre, 158 Civic Boulevard, Braamfontein",
        f"or uploaded electronically via the portal {portal_base}/mock-portal/tenders/COJ-EL-2026-045",
        "Maximum file size: 10 MB per document. Bid validity period: 90 days.",
    ], "City of Johannesburg · Group Supply Chain Management")
    page(doc, "PART T2: RETURNABLE SCHEDULES", [
        "The following mandatory returnable documents must be submitted:",
        "SBD 1 Invitation to bid", "SBD 3.1 Pricing schedule", "SBD 4 Bidder's disclosure",
        "SBD 6.1 Preference points claim form", "SBD 6.2 Declaration of local content",
        "Proof of registration on the Central Supplier Database (CSD)", "Tax Compliance Status PIN",
        "B-BBEE certificate or sworn affidavit", "CIPC company registration documents",
        "COIDA letter of good standing", "Certified ID copies of all directors", "Company profile",
        "Wireman's licence of the responsible Installation Electrician",
    ], "City of Johannesburg")
    page(doc, "PART C1/C2: PRICING DATA", ["Pricing instructions: rates exclude VAT. Complete the Bill of Quantities.",
                                            "Local content of at least 70% applies to steel poles."], "City of Johannesburg")
    page(doc, "PART C3: SCOPE OF WORK", ["Supply, deliver and install 150W LED luminaires on existing poles.",
                                          "All work to comply with SANS 10142-1 and SANS 10098.",
                                          "A Certificate of Compliance (CoC) must be issued on completion."], "City of Johannesburg")
    return doc.tobytes()


def boq_xlsx(qty_scale: float = 1.0) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BoQ"
    ws.append(["BILL OF QUANTITIES — " + TENDER_REF])
    ws.append([])
    ws.append(["Item", "Description", "Unit", "Qty", "Rate", "Amount"])
    rows = [("1", "150W LED street light luminaire IP66", "each", 120), ("2", "Galvanised steel pole 9m", "each", 40),
            ("3", "4mm x 4 core PVC SWA copper cable", "m", 1800), ("4", "Excavation for cable trench 0.8m deep", "m", 600)]
    for r in rows:
        ws.append([r[0], r[1], r[2], round(r[3] * qty_scale), None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def sbd1_flat() -> bytes:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.pdf.sbd import generate_template

    return generate_template("SBD 1", with_fields=False)


def tender_pack(portal_base: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("RFP_COJ_EL_2026_045.pdf", rfp(portal_base))
        zf.writestr("Annexures/BoQ_COJ_EL_2026_045.xlsx", boq_xlsx())
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z2:
            z2.writestr("SBD1_Invitation_to_Bid.pdf", sbd1_flat())
        zf.writestr("Annexures/SBD_forms.zip", inner.getvalue())
    return buf.getvalue()


def addendum() -> bytes:
    new_close = TODAY + timedelta(days=28)
    return pdf("ADDENDUM No. 1 — " + TENDER_REF, [
        f"Tender Number: {TENDER_REF}", "The closing date has been extended.",
        f"Closing Date: {new_close.strftime('%d %B %Y')} at 11:00", "All other conditions remain unchanged."], "City of Johannesburg")


def attendance_cert() -> bytes:
    doc = fitz.open()
    p = page(doc, "CERTIFICATE OF ATTENDANCE — COMPULSORY BRIEFING", [
        f"Tender Number: {TENDER_REF}", f"Company: {COMPANY}", "Representative: Sipho Johannes Dlamini",
        f"Date: {d(TODAY)}", "Signed: SCM Official"], "City of Johannesburg · Group SCM")
    stamp(p, (420, 600), TODAY, color=(0.75, 0.1, 0.1))
    return doc.tobytes()


def catalog_csv() -> str:
    return "\n".join([
        "sku_code,item_name,technical_specs,unit_of_measure,unit_price_excl_vat,lead_time_days,local_content_pct",
        "LED-150-IP66,LED street light luminaire 150W,150W LED luminaire IP66 street lighting,each,2450.00,14,35",
        "POLE-9M-GALV,Galvanised steel street light pole 9m,9m hot-dip galvanised steel pole,each,6800.00,21,95",
        "CAB-4C-4MM-SWA,PVC SWA copper cable 4 core 4mm,4mm x 4 core copper PVC SWA armoured cable,m,98.50,5,80",
        "CIV-TRENCH-08,Cable trench excavation 0.8m,excavation for cable trench 0.8m deep backfill,m,145.00,0,100",
        "LED-80-IP65,LED floodlight 80W,80W LED flood light IP65,each,1150.00,7,20",
    ]) + "\n"


def write_all(out: Path, portal_base: str = "http://localhost:8000") -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "cipc.pdf": cipc(), "csd.pdf": csd(), "sars_tcs.pdf": sars(TODAY + timedelta(days=200)),
        "sars_tcs_short_expiry.pdf": sars(TODAY + timedelta(days=30)),
        "bbbee_affidavit.pdf": affidavit(True), "bbbee_affidavit_unstamped.pdf": affidavit(False),
        "coida.pdf": coida(TODAY + timedelta(days=120)), "cidb.pdf": cidb(), "wireman.pdf": wireman(), "bank.pdf": bank(),
        "director_id_1.pdf": director_id(0), "director_id_2.pdf": director_id(1), "company_profile.pdf": company_profile(),
        "low_quality_scan.pdf": low_quality_scan(), "tender_pack.zip": tender_pack(portal_base), "addendum_1.pdf": addendum(),
        "boq_revised.xlsx": boq_xlsx(1.25), "attendance_certificate.pdf": attendance_cert(),
    }
    paths = {}
    for name, data in files.items():
        (out / name).write_bytes(data)
        paths[name] = out / name
    (out / "catalog.csv").write_text(catalog_csv())
    paths["catalog.csv"] = out / "catalog.csv"
    return paths


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "samples" / "out"
    base = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"
    for k, v in write_all(target, base).items():
        print(f"{k:32s} {v.stat().st_size:>8,d} bytes")
