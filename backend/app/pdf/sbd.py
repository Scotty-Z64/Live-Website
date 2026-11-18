"""Standard Bidding Documents: canonical slot map, AcroForm templates, and the hybrid filler.

Filling strategy (blueprint §7):
1. AcroForm first — detect interactive widgets and map each widget name to a canonical slot.
2. Coordinate overlay fallback — for flattened/scanned SBDs, locate the printed label on the page
   (text layer search) and draw the value in the answer area next to/below it.
Only verified DB values are injected. Declarations (YES/NO answers, signatures) are never
auto-answered; they are listed on the wet-ink signing map instead.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import fitz
from reportlab.lib.colors import black, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

FORMS = ["SBD 1", "SBD 3.1", "SBD 4", "SBD 6.1", "SBD 6.2"]

# canonical slot → (printed labels used on National Treasury SBDs, widget-name regex)
SLOTS: dict[str, tuple[list[str], str]] = {
    "bid_number": (["BID NUMBER"], r"bid.?(no|num)|tender.?(no|num|ref)"),
    "closing_date": (["CLOSING DATE"], r"closing.?date"),
    "closing_time": (["CLOSING TIME"], r"closing.?time"),
    "bid_description": (["DESCRIPTION"], r"^desc|bid.?desc"),
    "supplier_name": (["NAME OF BIDDER", "NAME OF SUPPLIER", "NAME OF TENDERER"], r"(name.?of.?(bidder|supplier|tenderer)|supplier.?name|bidder.?name)"),
    "trading_name": (["TRADING NAME"], r"trading.?name"),
    "registration_number": (["REGISTRATION NUMBER", "COMPANY REGISTRATION NUMBER"], r"(company.?)?reg(istration)?.?(no|num)"),
    "postal_address": (["POSTAL ADDRESS"], r"postal.?addr"),
    "street_address": (["STREET ADDRESS", "PHYSICAL ADDRESS"], r"(street|physical).?addr"),
    "telephone": (["TELEPHONE NUMBER"], r"tel(ephone)?"),
    "email": (["E-MAIL ADDRESS", "EMAIL ADDRESS"], r"e.?mail"),
    "contact_person": (["CONTACT PERSON"], r"contact.?person"),
    "vat_number": (["VAT REGISTRATION NUMBER"], r"vat"),
    "csd_number": (["CENTRAL SUPPLIER DATABASE No", "CSD NUMBER", "SUPPLIER NUMBER"], r"csd|maaa|supplier.?(no|num)"),
    "tcs_pin": (["TAX COMPLIANCE SYSTEM PIN", "TCS PIN"], r"tcs|tax.?(compliance.?)?pin"),
    "bbbee_level": (["B-BBEE STATUS LEVEL", "B-BBEE LEVEL OF CONTRIBUTION"], r"(bbbee|b.bbee|bee).?(status.?)?level"),
    "bbbee_instrument": (["B-BBEE STATUS LEVEL VERIFICATION CERTIFICATE / SWORN AFFIDAVIT"], r"(verification|affidavit|instrument)"),
    "total_bid_price": (["TOTAL BID PRICE", "TOTAL BID PRICE (INCL VAT)"], r"total.?(bid.?)?price"),
    "total_excl_vat": (["TOTAL EXCLUDING VAT", "SUB-TOTAL"], r"(total.?excl|sub.?total)"),
    "vat_amount": (["VAT (15%)", "VAT AMOUNT"], r"vat.?(amount|15)"),
    "preference_system": (["PREFERENCE POINT SYSTEM APPLICABLE"], r"pref(erence)?.?(system|point)"),
    "points_claimed": (["POINTS CLAIMED"], r"points.?claim"),
    "local_content_pct": (["LOCAL CONTENT %", "AVERAGE LOCAL CONTENT"], r"local.?content"),
    **{f"director_{i}_name": ([], rf"director.?{i}.?name") for i in range(1, 7)},
    **{f"director_{i}_id": ([], rf"director.?{i}.?id") for i in range(1, 7)},
    **{f"director_{i}_share": ([], rf"director.?{i}.?(share|pct)") for i in range(1, 7)},
}

FORM_LAYOUT: dict[str, dict] = {
    "SBD 1": {
        "title": "SBD 1 — PART A: INVITATION TO BID",
        "intro": "YOU ARE HEREBY INVITED TO BID FOR REQUIREMENTS OF THE (NAME OF DEPARTMENT / PUBLIC ENTITY).",
        "slots": ["bid_number", "closing_date", "closing_time", "bid_description", "supplier_name", "trading_name",
                  "registration_number", "postal_address", "street_address", "telephone", "email", "contact_person",
                  "vat_number", "csd_number", "tcs_pin", "bbbee_instrument", "bbbee_level", "total_bid_price"],
        "declarations": ["ARE YOU THE ACCREDITED REPRESENTATIVE IN SOUTH AFRICA FOR THE GOODS / SERVICES OFFERED?  YES / NO",
                         "ARE YOU A FOREIGN BASED SUPPLIER FOR THE GOODS / SERVICES OFFERED?  YES / NO"],
        "signatures": ["SIGNATURE OF BIDDER", "CAPACITY UNDER WHICH THIS BID IS SIGNED", "DATE"],
    },
    "SBD 3.1": {
        "title": "SBD 3.1 — PRICING SCHEDULE (FIRM PRICES)",
        "intro": "Prices are firm, in South African Rand, and include all delivery costs. Detailed BoQ pricing is attached.",
        "slots": ["bid_number", "supplier_name", "total_excl_vat", "vat_amount", "total_bid_price"],
        "declarations": ["Period required for delivery: ____________________ (wet ink)"],
        "signatures": ["SIGNATURE OF BIDDER", "DATE"],
    },
    "SBD 4": {
        "title": "SBD 4 — BIDDER'S DISCLOSURE",
        "intro": "Full details of directors / trustees / members / shareholders.",
        "slots": ["bid_number", "supplier_name", "registration_number"] + [f"director_{i}_{k}" for i in range(1, 7) for k in ("name", "id", "share")],
        "declarations": [
            "2.1 Is the bidder, or any of its directors / trustees / shareholders / members, employed by the state?  YES / NO",
            "2.2 Do you have any relationship with any person employed by the procuring institution?  YES / NO",
            "2.3 Does the bidder or any of its directors have any interest in any other related enterprise bidding?  YES / NO",
            "3.1 Did you discuss or agree on prices, quantities or bidding with any competitor?  YES / NO",
        ],
        "signatures": ["SIGNATURE", "NAME OF BIDDER", "POSITION", "DATE"],
    },
    "SBD 6.1": {
        "title": "SBD 6.1 — PREFERENCE POINTS CLAIM FORM (PPPFA REGULATIONS 2022)",
        "intro": "Points claimed for specific goals must be substantiated by a valid B-BBEE certificate or sworn affidavit.",
        "slots": ["bid_number", "supplier_name", "registration_number", "preference_system", "bbbee_level", "points_claimed", "csd_number"],
        "declarations": ["I/we certify that the points claimed are true and correct and that the enterprise qualifies for them."],
        "signatures": ["SIGNATURE(S) OF BIDDER(S)", "SURNAME AND NAME", "DATE", "ADDRESS"],
    },
    "SBD 6.2": {
        "title": "SBD 6.2 — DECLARATION CERTIFICATE FOR LOCAL PRODUCTION AND CONTENT",
        "intro": "Applicable to designated sectors only. Annex C (Local Content Declaration summary schedule) to be attached.",
        "slots": ["bid_number", "supplier_name", "local_content_pct"],
        "declarations": ["I, the undersigned, declare that the local content percentages above were calculated per SATS 1286:2011."],
        "signatures": ["SIGNATURE", "NAME (IN PRINT)", "DATE", "WITNESS"],
    },
}

LABEL_OF = {k: (v[0][0] if v[0] else k.replace("_", " ").upper()) for k, v in SLOTS.items()}


def generate_template(form: str, with_fields: bool = True, extra_table: list[list[str]] | None = None) -> bytes:
    """Render an SBD template. ``with_fields=False`` yields a flattened layout (labels only) —
    identical geometry, used to exercise the overlay path."""
    spec = FORM_LAYOUT[form]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    y = H - 22 * mm

    def header():
        nonlocal y
        c.setFont("Helvetica-Bold", 13)
        c.drawString(18 * mm, y, spec["title"])
        y -= 7 * mm
        c.setFont("Helvetica", 8.5)
        c.drawString(18 * mm, y, spec["intro"][:120])
        y -= 9 * mm

    header()
    form_key = form.replace(" ", "").replace(".", "_").lower()
    for slot in spec["slots"]:
        if y < 60 * mm:
            c.showPage()
            y = H - 22 * mm
            header()
        label = LABEL_OF[slot]
        c.setFont("Helvetica-Bold" if not slot.startswith("director_") else "Helvetica", 8)
        c.drawString(18 * mm, y + 1.5 * mm, label)
        c.setStrokeColor(HexColor("#999999"))
        c.rect(95 * mm, y - 1 * mm, 97 * mm, 6.5 * mm)
        if with_fields:
            c.acroForm.textfield(name=f"{form_key}.{slot}", x=95 * mm, y=y - 1 * mm, width=97 * mm, height=6.5 * mm,
                                 borderWidth=0, fontSize=8, fillColor=None, forceBorder=False)
        y -= 9 * mm
    if extra_table:
        c.setFont("Helvetica-Bold", 7.5)
        y -= 2 * mm
        for row in extra_table:
            if y < 45 * mm:
                c.showPage()
                y = H - 22 * mm
            xs = [18, 30, 118, 132, 148, 170]
            for x, cell in zip(xs, row):
                c.drawString(x * mm, y, str(cell)[:52])
            c.setFont("Helvetica", 7.5)
            y -= 5 * mm
    y -= 4 * mm
    c.setFont("Helvetica", 8)
    for d in spec["declarations"]:
        if y < 40 * mm:
            c.showPage()
            y = H - 22 * mm
        c.drawString(18 * mm, y, d[:130])
        y -= 7 * mm
    y -= 6 * mm
    for s in spec["signatures"]:
        if y < 22 * mm:
            c.showPage()
            y = H - 22 * mm
        c.setStrokeColor(black)
        c.line(95 * mm, y, 192 * mm, y)
        c.drawString(18 * mm, y + 1 * mm, s)
        y -= 11 * mm
    c.setFont("Helvetica-Oblique", 6.5)
    c.drawString(18 * mm, 10 * mm, f"{form} — layout reproduced for completion; the procuring entity's own issued form takes precedence.")
    c.save()
    return buf.getvalue()


def identify_form(text: str) -> str | None:
    for form in sorted(FORMS, key=len, reverse=True):
        num = form.split()[1].replace(".", r"\.")
        if re.search(rf"\b(?:SBD|MBD)\s?{num}\b(?![\d.])", text[:1500], re.I):
            return form
    return None


@dataclass
class FillResult:
    pdf: bytes
    method: str  # acroform | overlay | none
    filled: list[str] = field(default_factory=list)
    unmatched_widgets: list[str] = field(default_factory=list)
    missing_values: list[str] = field(default_factory=list)


def _slot_for_widget(name: str) -> str | None:
    tail = name.split(".")[-1].lower()
    if tail in SLOTS:
        return tail
    for slot, (_, rx) in SLOTS.items():
        if re.search(rx, tail, re.I):
            return slot
    return None


def fill(pdf_bytes: bytes, values: dict[str, str | None]) -> FillResult:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    widgets = [(p, w) for p in doc for w in (p.widgets() or [])]
    filled, unmatched, missing = [], [], []
    if widgets:
        for _, w in widgets:
            if w.field_type not in (fitz.PDF_WIDGET_TYPE_TEXT,):
                continue
            slot = _slot_for_widget(w.field_name or "")
            if not slot:
                unmatched.append(w.field_name)
                continue
            val = values.get(slot)
            if val in (None, ""):
                missing.append(slot)
                continue
            w.field_value = str(val)
            w.update()
            filled.append(slot)
        out = doc.tobytes(garbage=3, deflate=True)
        return FillResult(out, "acroform", filled, unmatched, sorted(set(missing)))

    # Overlay fallback for flattened forms. Longest labels claim their rectangles first so that a
    # short label ("B-BBEE STATUS LEVEL") never lands on a longer one that contains it.
    used: list[tuple[int, fitz.Rect]] = []
    order = sorted(SLOTS.items(), key=lambda kv: -max((len(l) for l in kv[1][0]), default=0))
    for slot, (labels, _) in order:
        val = values.get(slot)
        if not labels:
            continue
        placed = False
        for page in doc:
            for label in labels:
                hits = [h for h in page.search_for(label)
                        if not any(pn == page.number and (h & u).get_area() > 0 for pn, u in used)]
                if not hits:
                    continue
                r = hits[0]
                line = fitz.Rect(page.rect.x0, r.y0, page.rect.x1, r.y1)
                used.append((page.number, line))
                if val in (None, ""):
                    placed = True  # label reserved; nothing verified to write
                    break
                # Answer area: right of label if the line has room, else just below.
                x = max(r.x1 + 8, 270.0)  # 270pt ≈ 95 mm: the answer column on SBD layouts
                if x > page.rect.width - 60:
                    x, ybase = r.x0, r.y1 + 10
                else:
                    ybase = r.y1 - 1.5
                page.insert_text((x, ybase), str(val)[:90], fontsize=8, fontname="helv", color=(0, 0, 0.55))
                filled.append(slot)
                placed = True
                break
            if placed:
                break
        if val in (None, "") or not placed:
            missing.append(slot)
    # Director rows on flattened SBD 4: write a block under the disclosure heading if present.
    dir_vals = [(values.get(f"director_{i}_name"), values.get(f"director_{i}_id"), values.get(f"director_{i}_share")) for i in range(1, 7)]
    dir_vals = [d for d in dir_vals if d[0]]
    if dir_vals:
        for page in doc:
            hits = page.search_for("Full details of directors") or page.search_for("FULL DETAILS OF DIRECTORS")
            if hits:
                y = hits[0].y1 + 12
                for name, sid, share in dir_vals:
                    page.insert_text((hits[0].x0, y), f"{name}   ID {sid}   {share or ''}", fontsize=7.5, fontname="helv", color=(0, 0, 0.55))
                    y += 10
                filled.append("directors")
                break
    method = "overlay" if filled else "none"
    return FillResult(doc.tobytes(garbage=3, deflate=True), method, filled, [], sorted(set(missing)))


def signature_locations(pdf_bytes: bytes) -> list[dict]:
    """Pages/positions needing wet ink: signature lines and YES/NO declarations."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for i, page in enumerate(doc):
        for kw, kind in (("SIGNATURE", "FULL_SIGNATURE"), ("YES / NO", "DECLARATION_ANSWER"), ("Commissioner of Oaths", "COMMISSIONER")):
            hits = page.search_for(kw)
            for h in hits[:6]:
                out.append({"page_index": i, "kind": kind, "y_mm": round(h.y0 * 25.4 / 72, 1), "context": kw})
    return out
