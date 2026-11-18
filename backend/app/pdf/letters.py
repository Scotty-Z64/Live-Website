"""Letterhead documents rendered with WeasyPrint: Letter of Authority / Delegation for briefings and
the pre-filled B-BBEE sworn affidavit template. Every variable comes from verified DB values or
explicit user input — no generated prose in legal declarations."""
from __future__ import annotations

from datetime import date
from html import escape

from weasyprint import HTML

BASE_CSS = """
@page { size: A4; margin: 22mm 20mm 20mm 20mm; @bottom-center { content: counter(page) " / " counter(pages); font-size: 8pt; color: #666; } }
body { font-family: 'DejaVu Sans', Arial, sans-serif; font-size: 10.5pt; color: #111; line-height: 1.45; }
.lh { border-bottom: 2px solid #0f2a44; padding-bottom: 8px; margin-bottom: 18px; }
.lh h1 { font-size: 16pt; margin: 0; color: #0f2a44; }
.lh .meta { font-size: 8.5pt; color: #444; }
h2 { font-size: 12.5pt; margin: 18px 0 8px; }
table.kv { border-collapse: collapse; width: 100%; margin: 8px 0; }
table.kv td { border: 1px solid #bbb; padding: 5px 7px; vertical-align: top; }
table.kv td:first-child { width: 38%; background: #f3f5f8; font-weight: 600; }
.sig { margin-top: 34px; display: flex; gap: 40px; }
.sig div { flex: 1; border-top: 1px solid #000; padding-top: 4px; font-size: 9pt; }
.box { border: 1.5px dashed #888; height: 120px; margin-top: 10px; padding: 6px; font-size: 8.5pt; color: #666; }
.note { font-size: 8.5pt; color: #555; }
"""


def _letterhead(t: dict) -> str:
    parts = [f"Reg. No: {t.get('cipc_reg_number') or '—'}", f"CSD: {t.get('csd_maaa_number') or '—'}"]
    if t.get("vat_number"):
        parts.append(f"VAT: {t['vat_number']}")
    contact = " · ".join(x for x in [t.get("physical_address"), t.get("contact_phone"), t.get("contact_email")] if x)
    return (f"<div class='lh'><h1>{escape(t.get('legal_name') or '')}</h1>"
            f"<div class='meta'>{escape(' · '.join(parts))}</div><div class='meta'>{escape(contact)}</div></div>")


def _kv(rows: list[tuple[str, str | None]]) -> str:
    return "<table class='kv'>" + "".join(f"<tr><td>{escape(k)}</td><td>{escape(v or '')}</td></tr>" for k, v in rows) + "</table>"


def letter_of_authority(tenant: dict, tender: dict, rep: dict, signatory: dict, today: date | None = None) -> bytes:
    today = today or date.today()
    body = f"""
    {_letterhead(tenant)}
    <p>{today.strftime('%d %B %Y')}</p>
    <p>To: {escape(tender.get('issuing_entity') or 'The Procuring Entity')}<br/>Supply Chain Management</p>
    <h2>LETTER OF AUTHORITY / DELEGATION — COMPULSORY BRIEFING SESSION</h2>
    {_kv([
        ("Tender reference", tender.get("tender_reference")),
        ("Tender description", tender.get("title")),
        ("Briefing date & time", tender.get("briefing_when")),
        ("Venue / link", tender.get("briefing_venue_or_link")),
    ])}
    <p>I, the undersigned, <b>{escape(signatory.get('full_name', ''))}</b>, in my capacity as
    <b>{escape(signatory.get('capacity', 'Director'))}</b> of <b>{escape(tenant.get('legal_name') or '')}</b>
    (Registration No. {escape(tenant.get('cipc_reg_number') or '—')}, CSD {escape(tenant.get('csd_maaa_number') or '—')}),
    hereby authorise the person below to attend the compulsory briefing session for the above tender,
    to sign the attendance register and to receive the Certificate of Attendance on behalf of the company.</p>
    {_kv([
        ("Authorised representative", rep.get("full_name")),
        ("Identity / passport number", rep.get("id_number")),
        ("Position in company", rep.get("position")),
        ("Contact number", rep.get("phone")),
        ("E-mail", rep.get("email")),
    ])}
    <p class='note'>The representative must present the original of this letter and their identity document at the briefing.
    Online briefings: the representative must join with the registered name so that the attendance register reflects the bidder.</p>
    <div class='sig'><div>Signature of authorising officer<br/>{escape(signatory.get('full_name', ''))}</div><div>Date</div></div>
    <div class='sig'><div>Signature of authorised representative<br/>{escape(rep.get('full_name', ''))}</div><div>Company stamp</div></div>
    """
    return HTML(string=f"<html><head><style>{BASE_CSS}</style></head><body>{body}</body></html>").write_pdf()


def bbbee_affidavit_template(tenant: dict, deponent: dict, today: date | None = None) -> bytes:
    today = today or date.today()
    turnover = tenant.get("annual_turnover_zar")
    tier = "Exempted Micro Enterprise (EME)" if turnover is not None and float(turnover) <= 10_000_000 else "Qualifying Small Enterprise (QSE)"
    fy = deponent.get("financial_year_end") or ""
    body = f"""
    <h2 style='text-align:center'>SWORN AFFIDAVIT — B-BBEE {escape(tier.upper())}</h2>
    <p class='note' style='text-align:center'>In terms of the Codes of Good Practice on Black Economic Empowerment (Generic / Sector Codes)</p>
    <p>I, the undersigned, <b>{escape(deponent.get('full_name', ''))}</b>, Identity Number <b>{escape(deponent.get('id_number', ''))}</b>,
    hereby declare under oath as follows:</p>
    <ol>
    <li>The contents of this statement are to the best of my knowledge a true reflection of the facts.</li>
    <li>I am a member / director / owner of the following enterprise and am duly authorised to act on its behalf:</li>
    </ol>
    {_kv([
        ("Enterprise name", tenant.get("legal_name")),
        ("Trading name", tenant.get("trading_name")),
        ("Registration number", tenant.get("cipc_reg_number")),
        ("Enterprise physical address", tenant.get("physical_address")),
        ("Type of entity", "Private Company (Pty) Ltd"),
        ("Nature of business", ", ".join(tenant.get("sectors") or [])),
        ("Annual total revenue", f"R {float(turnover):,.2f}" if turnover is not None else ""),
        ("Financial year end", fy),
        ("Black ownership", f"{tenant.get('black_ownership_pct') or ''}%"),
        ("Black women ownership", f"{tenant.get('black_women_ownership_pct') or ''}%"),
        ("B-BBEE status level", f"Level {tenant.get('bee_level') or ''}"),
    ])}
    <ol start='3'>
    <li>I know and understand the contents of this affidavit and I have no objection to taking the prescribed oath.</li>
    <li>I consider the oath binding on my conscience and on the owners of the enterprise which I represent.</li>
    </ol>
    <div class='sig'><div>Deponent signature</div><div>Date</div></div>
    <h2>COMMISSIONER OF OATHS</h2>
    <p class='note'>I certify that the deponent has acknowledged that he/she knows and understands the contents of this affidavit,
    which was signed and sworn to before me at ______________________ on this ____ day of ______________ 20__.</p>
    <div class='box'>Commissioner of Oaths stamp (full names, designation, area, business address)</div>
    <div class='sig'><div>Commissioner of Oaths signature</div><div>Date</div></div>
    <p class='note'>Valid for 12 months from the date commissioned. Generated {today.isoformat()} from verified TenderSync vault data.</p>
    """
    return HTML(string=f"<html><head><style>{BASE_CSS}</style></head><body>{body}</body></html>").write_pdf()


def addendum_acknowledgement(tenant: dict, tender: dict, addenda: list[dict]) -> bytes:
    rows = "".join(
        f"<tr><td>{escape(a.get('filename', ''))}</td><td>{escape((a.get('detected_at') or '')[:16])}</td>"
        f"<td>{escape(a.get('acknowledged_by', ''))}</td><td>{escape((a.get('acknowledged_at') or '')[:16])}</td>"
        f"<td style='font-family:monospace;font-size:7pt'>{escape((a.get('new_sha256') or '')[:16])}…</td></tr>"
        for a in addenda
    )
    body = f"""{_letterhead(tenant)}
    <h2>ACKNOWLEDGEMENT OF ADDENDA — {escape(tender.get('tender_reference') or '')}</h2>
    <p>The bidder acknowledges receipt and incorporation of the following addenda / errata into this bid:</p>
    <table class='kv'><tr><td>File</td><td>Detected</td><td>Signed off by</td><td>Signed off at</td><td>SHA-256</td></tr>{rows}</table>
    <div class='sig'><div>Authorised signatory</div><div>Date</div></div>"""
    return HTML(string=f"<html><head><style>{BASE_CSS}</style></head><body>{body}</body></html>").write_pdf()


def html_pdf(html_body: str) -> bytes:
    return HTML(string=f"<html><head><style>{BASE_CSS}</style></head><body>{html_body}</body></html>").write_pdf()
