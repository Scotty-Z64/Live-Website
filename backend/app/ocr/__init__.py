"""Hybrid text extraction.

1. Born-digital PDFs: the embedded text layer is read deterministically with PyMuPDF
   (confidence 1.0 — no recognition is involved).
2. Scans / photos / image-only PDFs: Azure AI Document Intelligence (prebuilt-layout for forms
   and BoQ tables, prebuilt-read for plain letters). Confidence = mean word confidence.
3. If Azure is not configured, scanned input gets confidence 0.0 → LOW_OCR_CONFIDENCE, i.e. the
   platform asks for a better document instead of guessing values.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field

import fitz  # PyMuPDF
import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

AZURE_API_VERSION = "2024-11-30"
MIN_TEXT_CHARS_PER_PAGE = 40


@dataclass
class OcrResult:
    text: str
    pages: list[str]
    confidence: float
    engine: str
    tables: list[list[list[str]]] = field(default_factory=list)
    page_count: int = 0

    @property
    def low_confidence(self) -> bool:
        return self.confidence < get_settings().ocr_confidence_threshold


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def image_to_pdf(data: bytes) -> bytes:
    img = fitz.open(stream=data)
    pdf_bytes = img.convert_to_pdf()
    return pdf_bytes


def text_layer(data: bytes) -> tuple[list[str], int]:
    doc = fitz.open(stream=data, filetype="pdf")
    pages = [p.get_text("text") for p in doc]
    return pages, doc.page_count


def _azure_analyze(data: bytes, model: str) -> OcrResult:
    s = get_settings()
    base = s.azure_doc_intel_endpoint.rstrip("/")
    url = f"{base}/documentintelligence/documentModels/{model}:analyze?api-version={AZURE_API_VERSION}"
    headers = {"Ocp-Apim-Subscription-Key": s.azure_doc_intel_key, "Content-Type": "application/octet-stream"}
    with httpx.Client(timeout=60) as client:
        r = client.post(url, content=data, headers=headers)
        r.raise_for_status()
        op = r.headers["Operation-Location"]
        deadline = time.time() + 180
        while True:
            pr = client.get(op, headers={"Ocp-Apim-Subscription-Key": s.azure_doc_intel_key})
            pr.raise_for_status()
            body = pr.json()
            if body.get("status") == "succeeded":
                break
            if body.get("status") == "failed" or time.time() > deadline:
                raise RuntimeError(f"Azure Document Intelligence analyze failed: {body.get('error')}")
            time.sleep(1.5)
    res = body["analyzeResult"]
    content = res.get("content", "")
    pages, confs = [], []
    for p in res.get("pages", []):
        words = p.get("words", [])
        confs.extend(w.get("confidence", 0.0) for w in words)
        spans = p.get("spans", [])
        pages.append("".join(content[sp["offset"] : sp["offset"] + sp["length"]] for sp in spans))
    tables = []
    for t in res.get("tables", []):
        grid = [["" for _ in range(t["columnCount"])] for _ in range(t["rowCount"])]
        for c in t.get("cells", []):
            grid[c["rowIndex"]][c["columnIndex"]] = c.get("content", "")
        tables.append(grid)
    conf = sum(confs) / len(confs) if confs else 0.0
    return OcrResult(content, pages, conf, f"azure:{model}", tables, len(pages))


def extract(data: bytes, prefer_layout: bool = True) -> OcrResult:
    s = get_settings()
    pdf = data if is_pdf(data) else image_to_pdf(data)
    pages, count = text_layer(pdf)
    total_chars = sum(len(p.strip()) for p in pages)
    if count and total_chars >= MIN_TEXT_CHARS_PER_PAGE * count * 0.5:
        return OcrResult("\n".join(pages), pages, 1.0, "pdf-text-layer", [], count)

    if s.azure_doc_intel_endpoint and s.azure_doc_intel_key:
        try:
            return _azure_analyze(pdf, "prebuilt-layout" if prefer_layout else "prebuilt-read")
        except Exception as exc:  # noqa: BLE001
            log.exception("Azure OCR failed")
            return OcrResult("\n".join(pages), pages, 0.0, f"azure-error:{exc}", [], count)
    return OcrResult("\n".join(pages), pages, 0.0, "no-ocr-engine", [], count)


def render_page_png(data: bytes, page_no: int = 0, dpi: int = 150) -> bytes:
    pdf = data if is_pdf(data) else image_to_pdf(data)
    doc = fitz.open(stream=pdf, filetype="pdf")
    pix = doc[min(page_no, doc.page_count - 1)].get_pixmap(dpi=dpi)
    return pix.tobytes("png")


def xlsx_rows(data: bytes) -> list[list[str]]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    rows: list[list[str]] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in row]
            if any(cells):
                rows.append(cells)
    return rows
