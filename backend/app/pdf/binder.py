"""Mode B — Three-pass hardcopy binder compiler (blueprint §8).

Pass 1  Geometry normalisation: every page of every attachment (landscape certificates, phone
        scans, SBD forms) is placed on a uniform A4 portrait page, rotated/scaled to fit.
Pass 2  Dry-run offsets: cover + index + sections are assembled in memory to compute exact
        page offsets (index length is iterated until stable).
Pass 3  Rendering: continuous "Page X of Y" + Bates footers on every page, hyperlinked master
        index, printable tab dividers, a lever-arch spine insert sized from the page count, and a
        wet-ink signing map. Output: a ZIP with the four deliverables.
"""
from __future__ import annotations

import io
import math
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import fitz

A4_W, A4_H = 595.276, 841.890  # points
MM = 72 / 25.4
MARGIN_TOP, MARGIN_SIDE, MARGIN_BOTTOM = 12 * MM, 12 * MM, 16 * MM
INDEX_ROWS_PER_PAGE = 34

PAPER_CALIPER_MM = 0.10  # 80 gsm office paper
DIVIDER_CALIPER_MM = 0.20  # 160 gsm tab dividers
LEVER_ARCH_SPINES_MM = [(50, 35.0), (75, 55.0), (80, 60.0)]  # (spine width, usable capacity)


@dataclass
class Item:
    title: str
    data: bytes  # PDF or image bytes
    needs_signature: bool = False
    page_count: int = 0
    start_page: int = 0


@dataclass
class Section:
    title: str
    items: list[Item] = field(default_factory=list)
    start_page: int = 0
    page_count: int = 0


@dataclass
class BinderMeta:
    bidder_name: str
    tender_reference: str
    tender_title: str
    issuing_entity: str
    closing: str
    bates_prefix: str
    initial_every_page: bool = False
    tender_box_address: str | None = None


# ── Pass 1 ──────────────────────────────────────────────────────────────────────────────────
def _as_pdf(data: bytes) -> fitz.Document:
    if data[:5] == b"%PDF-":
        return fitz.open(stream=data, filetype="pdf")
    img = fitz.open(stream=data)
    return fitz.open(stream=img.convert_to_pdf(), filetype="pdf")


def normalise(data: bytes) -> fitz.Document:
    src = _as_pdf(data)
    out = fitz.open()
    box = fitz.Rect(MARGIN_SIDE, MARGIN_TOP, A4_W - MARGIN_SIDE, A4_H - MARGIN_BOTTOM)
    for i, page in enumerate(src):
        r = page.rect
        landscape = (r.width > r.height) ^ (page.rotation in (90, 270))
        dst = out.new_page(width=A4_W, height=A4_H)
        # show_pdf_page keeps aspect ratio and fits into ``box``; rotate landscape pages to portrait.
        dst.show_pdf_page(box, src, i, rotate=90 if landscape else 0, keep_proportion=True)
    return out


# ── Pass 2 ──────────────────────────────────────────────────────────────────────────────────
def plan(sections: list[Section], normalised: dict[int, fitz.Document]) -> tuple[int, int]:
    entries = sum(1 + len(s.items) for s in sections)
    index_pages = max(1, math.ceil(entries / INDEX_ROWS_PER_PAGE))
    while True:
        page = 1 + index_pages + 1  # cover + index, next page is first content page
        for s in sections:
            s.start_page = page
            for it in s.items:
                it.page_count = normalised[id(it)].page_count
                it.start_page = page
                page += it.page_count
            s.page_count = page - s.start_page
        total = page - 1
        needed = max(1, math.ceil(entries / INDEX_ROWS_PER_PAGE))
        if needed == index_pages:
            return index_pages, total
        index_pages = needed


# ── Pass 3 ──────────────────────────────────────────────────────────────────────────────────
def safe(text: str | None) -> str:
    """Base-14 PDF fonts are Latin-1 only: map common typographic characters, drop the rest."""
    t = (text or "").translate(str.maketrans({"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "•": "·", "…": "..."}))
    return t.encode("latin-1", "replace").decode("latin-1")


def _cover(doc: fitz.Document, m: BinderMeta, total: int) -> None:
    p = doc.new_page(width=A4_W, height=A4_H)
    p.draw_rect(fitz.Rect(0, 0, A4_W, 120), color=None, fill=(0.06, 0.16, 0.27))
    p.insert_textbox(fitz.Rect(40, 40, A4_W - 40, 110), "BID SUBMISSION", fontsize=26, fontname="hebo", color=(1, 1, 1))
    y = 170
    for label, val in [("Tender reference", m.tender_reference), ("Description", m.tender_title), ("Procuring entity", m.issuing_entity),
                       ("Closing date", m.closing), ("Bidder", m.bidder_name), ("Total pages", str(total))]:
        p.insert_text((40, y), label.upper(), fontsize=9, fontname="helv", color=(0.35, 0.35, 0.35))
        rect = fitz.Rect(40, y + 6, A4_W - 40, y + 60)
        p.insert_textbox(rect, safe(val) or "-", fontsize=14, fontname="hebo")
        y += 66
    if m.tender_box_address:
        p.insert_text((40, A4_H - 90), "DELIVER TO TENDER BOX:", fontsize=9, fontname="hebo")
        p.insert_textbox(fitz.Rect(40, A4_H - 84, A4_W - 40, A4_H - 40), safe(m.tender_box_address), fontsize=10, fontname="helv")


def _index(doc: fitz.Document, sections: list[Section], index_pages: int) -> list[tuple[int, fitz.Rect, int]]:
    rows = []
    for si, s in enumerate(sections, 1):
        rows.append((f"TAB {si}  {s.title.upper()}", s.start_page, True))
        for it in s.items:
            rows.append((f"      {it.title}" + ("   [SIGN]" if it.needs_signature else ""), it.start_page, False))
    links = []
    for pi in range(index_pages):
        p = doc.new_page(width=A4_W, height=A4_H)
        p.insert_text((40, 60), "MASTER INDEX" + (f" (cont.)" if pi else ""), fontsize=18, fontname="hebo")
        y = 95
        for text, page_no, bold in rows[pi * INDEX_ROWS_PER_PAGE:(pi + 1) * INDEX_ROWS_PER_PAGE]:
            font = "hebo" if bold else "helv"
            text = safe(text)
            p.insert_text((40, y), text[:85], fontsize=10 if bold else 9.5, fontname=font)
            num = f"{page_no}"
            p.insert_text((A4_W - 40 - fitz.get_text_length(num, fontname=font, fontsize=10), y), num, fontsize=10, fontname=font)
            # dotted leader
            p.draw_line(fitz.Point(40 + fitz.get_text_length(text[:85], fontname=font, fontsize=10) + 6, y - 2),
                        fitz.Point(A4_W - 50 - fitz.get_text_length(num, fontname=font, fontsize=10), y - 2),
                        color=(0.7, 0.7, 0.7), dashes="[1 3] 0", width=0.6)
            links.append((doc.page_count - 1, fitz.Rect(36, y - 11, A4_W - 36, y + 4), page_no))
            y += 21
        p.insert_text((40, A4_H - 40), "[SIGN] = requires wet-ink signature - see 04_Wet_Ink_Signing_Map.pdf", fontsize=8, fontname="helv",
                      color=(0.4, 0.4, 0.4))
    return links


def _stamp_footers(doc: fitz.Document, m: BinderMeta) -> None:
    total = doc.page_count
    for i, p in enumerate(doc):
        y = A4_H - 7 * MM
        p.draw_line(fitz.Point(MARGIN_SIDE, y - 10), fitz.Point(A4_W - MARGIN_SIDE, y - 10), color=(0.6, 0.6, 0.6), width=0.4)
        p.insert_text((MARGIN_SIDE, y), safe(f"{m.bidder_name[:40]} · {m.tender_reference[:40]}"), fontsize=7, fontname="helv", color=(0.3, 0.3, 0.3))
        label = f"Page {i + 1} of {total}"
        p.insert_text(((A4_W - fitz.get_text_length(label, fontname="hebo", fontsize=8)) / 2, y), label, fontsize=8, fontname="hebo")
        bates = f"{m.bates_prefix}-{i + 1:06d}"
        p.insert_text((A4_W - MARGIN_SIDE - fitz.get_text_length(bates, fontname="cour", fontsize=7), y), bates, fontsize=7, fontname="cour")


def tab_dividers(sections: list[Section], m: BinderMeta) -> bytes:
    doc = fitz.open()
    tab_h = (A4_H - 80) / max(len(sections), 1)
    tab_h = min(tab_h, 110)
    for i, s in enumerate(sections):
        p = doc.new_page(width=A4_W, height=A4_H)
        y0 = 40 + i * tab_h
        p.draw_rect(fitz.Rect(A4_W - 70, y0, A4_W, y0 + tab_h - 6), color=(0.06, 0.16, 0.27), fill=(0.06, 0.16, 0.27))
        p.insert_textbox(fitz.Rect(A4_W - 66, y0 + 8, A4_W - 4, y0 + tab_h - 10), f"{i + 1}", fontsize=28, fontname="hebo",
                         color=(1, 1, 1), align=fitz.TEXT_ALIGN_CENTER)
        p.insert_text((60, 300), f"TAB {i + 1}", fontsize=22, fontname="hebo", color=(0.06, 0.16, 0.27))
        p.insert_textbox(fitz.Rect(60, 320, A4_W - 100, 460), safe(s.title.upper()), fontsize=30, fontname="hebo")
        p.insert_text((60, 490), f"Binder pages {s.start_page}-{s.start_page + s.page_count - 1}", fontsize=12, fontname="helv")
        p.insert_text((60, A4_H - 50), safe(f"{m.tender_reference} · {m.bidder_name}"), fontsize=9, fontname="helv", color=(0.4, 0.4, 0.4))
    return doc.tobytes(garbage=3, deflate=True)


def spine_spec(total_pages: int, dividers: int, duplex: bool = False) -> dict:
    sheets = math.ceil(total_pages / 2) if duplex else total_pages
    thickness = round(sheets * PAPER_CALIPER_MM + dividers * DIVIDER_CALIPER_MM, 1)
    for spine, capacity in LEVER_ARCH_SPINES_MM:
        if thickness <= capacity:
            return {"sheets": sheets, "content_thickness_mm": thickness, "spine_width_mm": spine, "volumes": 1,
                    "label_width_mm": spine - 12, "label_height_mm": 190}
    spine, capacity = LEVER_ARCH_SPINES_MM[-1]
    vols = math.ceil(thickness / capacity)
    return {"sheets": sheets, "content_thickness_mm": thickness, "spine_width_mm": spine, "volumes": vols,
            "label_width_mm": spine - 12, "label_height_mm": 190}


def spine_label(spec: dict, m: BinderMeta) -> bytes:
    doc = fitz.open()
    for vol in range(1, spec["volumes"] + 1):
        p = doc.new_page(width=A4_W, height=A4_H)
        w, h = spec["label_width_mm"] * MM, spec["label_height_mm"] * MM
        x0, y0 = (A4_W - w) / 2, (A4_H - h) / 2
        r = fitz.Rect(x0, y0, x0 + w, y0 + h)
        p.draw_rect(r, color=(0, 0, 0), width=0.8)
        p.draw_rect(fitz.Rect(x0, y0, x0 + w, y0 + 22 * MM), color=None, fill=(0.06, 0.16, 0.27))
        # crop marks
        for cx, cy in ((r.x0, r.y0), (r.x1, r.y0), (r.x0, r.y1), (r.x1, r.y1)):
            p.draw_line(fitz.Point(cx - 14, cy), fitz.Point(cx - 4 if cx == r.x0 else cx + 14, cy), width=0.4)
            p.draw_line(fitz.Point(cx, cy - 14), fitz.Point(cx, cy - 4 if cy == r.y0 else cy + 14), width=0.4)
        p.insert_textbox(fitz.Rect(x0 + 2, y0 + 4, x0 + w - 2, y0 + 20 * MM), "BID", fontsize=14, fontname="hebo",
                         color=(1, 1, 1), align=fitz.TEXT_ALIGN_CENTER)
        # vertical text along the spine (rotate=90 reads bottom-to-top)
        text = safe(f"{m.tender_reference}  ·  {m.bidder_name}" + (f"  ·  VOL {vol}/{spec['volumes']}" if spec["volumes"] > 1 else ""))
        fs = min(16, (w - 8) * 0.55)
        tb = fitz.Rect(x0 + 4, y0 + 24 * MM, x0 + w - 4, y0 + h - 6)
        p.insert_textbox(tb, text, fontsize=fs, fontname="hebo", rotate=90, align=fitz.TEXT_ALIGN_CENTER)
        p.insert_text((40, 50), f"Lever-arch spine insert - cut along crop marks (actual size {spec['label_width_mm']} x {spec['label_height_mm']} mm)",
                      fontsize=9, fontname="helv")
        p.insert_text((40, 64), f"{spec['sheets']} sheets = approx. {spec['content_thickness_mm']} mm of paper -> {spec['spine_width_mm']} mm lever-arch file"
                      + (f", {spec['volumes']} volumes" if spec["volumes"] > 1 else ""), fontsize=9, fontname="helv")
    return doc.tobytes(garbage=3, deflate=True)


def signing_map(final: fitz.Document, sections: list[Section], m: BinderMeta) -> tuple[bytes, list[dict]]:
    rows: list[dict] = []
    first_content = min((s.start_page for s in sections), default=1) - 1
    for i, page in enumerate(final):
        if i < first_content:  # cover + index carry no signature blocks of their own
            continue
        for kw, kind in (("SIGNATURE", "FULL SIGNATURE"), ("Signature", "FULL SIGNATURE"), ("YES / NO", "DECLARATION (YES/NO)"),
                         ("Commissioner of Oaths stamp", "COMMISSIONER STAMP")):
            for h in page.search_for(kw)[:4]:
                rows.append({"page": i + 1, "requirement": kind, "y_mm": round(h.y0 / MM), "text": kw})
    # de-duplicate (case variants on the same line)
    seen, uniq = set(), []
    for r in rows:
        k = (r["page"], r["requirement"], r["y_mm"] // 3)
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    def section_of(pg: int) -> str:
        for s in sections:
            if s.start_page <= pg < s.start_page + s.page_count:
                for it in s.items:
                    if it.start_page <= pg < it.start_page + it.page_count:
                        return f"{s.title} > {it.title}"
                return s.title
        return "Cover / Index"

    doc = fitz.open()
    p = doc.new_page(width=A4_W, height=A4_H)
    p.insert_text((40, 55), "WET-INK SIGNING MAP", fontsize=18, fontname="hebo")
    p.insert_text((40, 75), safe(f"{m.tender_reference} · {m.bidder_name} · {final.page_count} pages"), fontsize=9, fontname="helv")
    y = 100
    if m.initial_every_page:
        p.insert_textbox(fitz.Rect(40, y - 10, A4_W - 40, y + 20),
                         f"The tender requires INITIALS ON EVERY PAGE: initial pages 1-{final.page_count} at the bottom-right corner.",
                         fontsize=10, fontname="hebo", color=(0.6, 0.1, 0.1))
        y += 30
    header = [("Page", 40), ("Requirement", 85), ("Position", 215), ("Document", 285)]
    for t, x in header:
        p.insert_text((x, y), t, fontsize=9, fontname="hebo")
    y += 14
    for r in sorted(uniq, key=lambda r: (r["page"], r["y_mm"])):
        if y > A4_H - 50:
            p = doc.new_page(width=A4_W, height=A4_H)
            y = 55
        r["document"] = section_of(r["page"])
        p.insert_text((40, y), str(r["page"]), fontsize=9, fontname="helv")
        p.insert_text((85, y), r["requirement"], fontsize=9, fontname="helv")
        p.insert_text((215, y), f"{r['y_mm']} mm from top", fontsize=9, fontname="helv")
        p.insert_text((285, y), safe(r["document"])[:55], fontsize=8.5, fontname="helv")
        y += 14
    p.insert_text((40, A4_H - 40), "Tick each row once signed. Sign in blue ink. Do not use correction fluid.", fontsize=8, fontname="helv")
    return doc.tobytes(garbage=3, deflate=True), uniq


def compile_binder(meta: BinderMeta, sections: list[Section]) -> dict:
    sections = [s for s in sections if s.items]
    # Pass 1
    normalised = {id(it): normalise(it.data) for s in sections for it in s.items}
    # Pass 2
    index_pages, total = plan(sections, normalised)
    # Pass 3
    final = fitz.open()
    _cover(final, meta, total)
    links = _index(final, sections, index_pages)
    for s in sections:
        for it in s.items:
            final.insert_pdf(normalised[id(it)])
    assert final.page_count == total, (final.page_count, total)
    for page_idx, rect, target in links:
        final[page_idx].insert_link({"kind": fitz.LINK_GOTO, "from": rect, "page": target - 1, "to": fitz.Point(0, 0)})
    toc = [[1, "Cover", 1], [1, "Master Index", 2]]
    for si, s in enumerate(sections, 1):
        toc.append([1, f"Tab {si} - {s.title}", s.start_page])
        toc += [[2, it.title, it.start_page] for it in s.items]
    final.set_toc(toc)
    _stamp_footers(final, meta)
    final.set_metadata({"title": f"Bid {meta.tender_reference}", "author": meta.bidder_name, "subject": meta.tender_title,
                        "creator": "TenderSync SA", "creationDate": fitz.get_pdf_now()})
    master = final.tobytes(garbage=4, deflate=True)
    tabs = tab_dividers(sections, meta)
    spec = spine_spec(total, len(sections))
    spine = spine_label(spec, meta)
    sign_pdf, sign_rows = signing_map(final, sections, meta)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("01_Master_Bid_Submission.pdf", master)
        zf.writestr("02_Printable_Tab_Dividers.pdf", tabs)
        zf.writestr("03_Lever_Arch_Spine_Label.pdf", spine)
        zf.writestr("04_Wet_Ink_Signing_Map.pdf", sign_pdf)
    return {
        "zip": buf.getvalue(), "master": master, "tabs": tabs, "spine": spine, "signing_map": sign_pdf,
        "total_pages": total, "index_pages": index_pages, "spine_spec": spec, "signing_rows": sign_rows,
        "sections": [{"title": s.title, "start_page": s.start_page, "page_count": s.page_count,
                      "items": [{"title": i.title, "start_page": i.start_page, "page_count": i.page_count} for i in s.items]} for s in sections],
        "compiled_at": datetime.now(timezone.utc).isoformat(),
    }
