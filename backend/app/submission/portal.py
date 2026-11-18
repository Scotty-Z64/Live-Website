"""Mode A — Digital portal submission worker (Playwright).

The worker logs in with credentials supplied at submission time (encrypted end-to-end through the
queue, never persisted), attaches each required schedule to the portal's upload slots, presses the
final submit, and then PAUSES at the OTP/2FA step. The human enters the OTP in TenderSync; the
worker types it, captures the confirmation page (full-page screenshot), portal reference number,
timestamp and the SHA-256 of the uploaded payload.

Portal selector maps are data (``PORTAL_ADAPTERS`` / ``SUBMISSION_ADAPTERS_FILE``). Only adapters
marked ``calibrated`` are allowed to run; real procurement portals must be calibrated against a
live supplier account before use.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

import fitz

from ..config import get_settings
from ..core.crypto import decrypt_pii, sha256_bytes
from ..core.events import bid_channel, get_redis, publish
from ..core.storage import get_storage, tenant_key
from ..db import tenant_session
from ..models import AuditEvent, BidPackage, Tender

log = logging.getLogger(__name__)

PORTAL_ADAPTERS: dict[str, dict] = {
    "mock": {
        "calibrated": True,
        "login_url": "{base}/mock-portal/login",
        "username": "input[name=username]", "password": "input[name=password]", "login_submit": "button[type=submit]",
        "tender_url": "{base}/mock-portal/tenders/{reference}",
        "upload_slots": {"SBD 1": "input[name=sbd1]", "SBD 3.1": "input[name=sbd31]", "SBD 4": "input[name=sbd4]",
                         "SBD 6.1": "input[name=sbd61]", "SBD 6.2": "input[name=sbd62]", "SUPPORTING": "input[name=supporting]"},
        "final_submit": "button#submit-bid",
        "otp_input": "input[name=otp]", "otp_submit": "button#confirm-otp",
        "confirmation": "#confirmation", "reference_regex": r"([A-Z]{3}-\d{4}-\d{6})",
        "max_file_mb": 10,
    },
    "etenders": {"calibrated": False, "notes": "eTenders publishes opportunities; submission happens on the entity's own channel."},
    "transnet": {"calibrated": False, "notes": "Requires a supplier account to capture selectors."},
    "eskom": {"calibrated": False, "notes": "Requires a supplier account to capture selectors."},
}


def adapters() -> dict[str, dict]:
    path = os.environ.get("SUBMISSION_ADAPTERS_FILE")
    out = dict(PORTAL_ADAPTERS)
    if path and os.path.exists(path):
        with open(path) as fh:
            out.update(json.load(fh))
    if get_settings().is_production:
        out.pop("mock", None)
    return out


def adapter_for(tender: Tender) -> tuple[str, dict]:
    url = (tender.portal_url or "").lower()
    table = adapters()
    if "/mock-portal" in url and "mock" in table:
        return "mock", table["mock"]
    key = tender.source_portal if tender.source_portal in table else None
    if not key:
        for k in table:
            if k in url:
                key = k
                break
    if not key:
        raise LookupError("No submission adapter for this portal")
    return key, table[key]


def compress_pdf(data: bytes, max_mb: float) -> tuple[bytes, dict]:
    """Deflate + garbage-collect; if still over the limit, downsample pages to 150→110 DPI JPEG.
    PDF/A-1b conversion is performed with Ghostscript when it is installed."""
    import shutil
    import subprocess
    import tempfile

    doc = fitz.open(stream=data, filetype="pdf")
    out = doc.tobytes(garbage=4, deflate=True, deflate_images=True, clean=True)
    info = {"original_bytes": len(data), "pdfa": False, "downsampled_dpi": None}
    for dpi in (150, 110):
        if len(out) <= max_mb * 1024 * 1024:
            break
        rast = fitz.open()
        for p in fitz.open(stream=out, filetype="pdf"):
            pix = p.get_pixmap(dpi=dpi)
            np = rast.new_page(width=p.rect.width, height=p.rect.height)
            np.insert_image(np.rect, stream=pix.tobytes("jpeg", jpg_quality=70))
        out = rast.tobytes(garbage=4, deflate=True)
        info["downsampled_dpi"] = dpi
    gs = shutil.which("gs")
    if gs:
        with tempfile.TemporaryDirectory() as td:
            src, dst = os.path.join(td, "in.pdf"), os.path.join(td, "out.pdf")
            open(src, "wb").write(out)
            r = subprocess.run([gs, "-dPDFA=1", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE", "-sColorConversionStrategy=RGB",
                                "-dPDFACompatibilityPolicy=1", "-sDEVICE=pdfwrite", f"-sOutputFile={dst}", src],
                               capture_output=True, timeout=180)
            if r.returncode == 0 and os.path.getsize(dst) <= max_mb * 1024 * 1024:
                out = open(dst, "rb").read()
                info["pdfa"] = "PDF/A-1b"
    info["final_bytes"] = len(out)
    return out, info


def build_payload(session, bid: BidPackage, max_mb: float) -> dict[str, tuple[str, bytes, dict]]:  # noqa: ANN001
    """slot → (filename, bytes, compression info). SBDs map to their own slots; everything else is
    merged into one SUPPORTING pack."""
    from ..bids.service import build_sections

    storage = get_storage()
    sections = build_sections(session, bid)
    payload = {}
    for form, o in bid.sbd_outputs.items():
        data, info = compress_pdf(storage.get(o["s3_key"]), max_mb)
        payload[form] = (f"{form.replace(' ', '_')}.pdf", data, info)
    merged = fitz.open()
    from ..pdf.binder import normalise

    for s in sections:
        if s.title == "Standard Bidding Documents":
            continue
        for it in s.items:
            merged.insert_pdf(normalise(it.data))
    if merged.page_count:
        data, info = compress_pdf(merged.tobytes(), max_mb)
        payload["SUPPORTING"] = ("Supporting_Documents.pdf", data, info)
    return payload


def otp_key(bid_id: str) -> str:
    return f"ts:otp:{bid_id}"


def submit(tenant_id: str, bid_id: str, encrypted_credentials: str) -> dict:
    from playwright.sync_api import sync_playwright

    s = get_settings()
    creds = json.loads(decrypt_pii(encrypted_credentials, "portal-creds"))
    storage = get_storage()
    with tenant_session(tenant_id) as session:
        bid = session.get(BidPackage, uuid.UUID(bid_id))
        tender = session.get(Tender, bid.tender_id)
        key, ad = adapter_for(tender)
        if not ad.get("calibrated"):
            raise RuntimeError(f"Submission adapter '{key}' is not calibrated: {ad.get('notes', '')}")
        max_mb = float((tender.preflight or {}).get("fields", {}).get("portal_max_mb") or ad.get("max_file_mb", 10))
        payload = build_payload(session, bid, max_mb)
        payload_hash = sha256_bytes(b"".join(sha256_bytes(v[1]).encode() for _, v in sorted(payload.items())))
        bid.status = "SUBMITTING"
        session.flush()
        publish(bid_channel(tenant_id, bid_id), "submission.status", {"status": "SUBMITTING"})
        base = s.app_base_url.rstrip("/")
        fmt = {"base": base, "reference": tender.tender_reference.replace("/", "-")}
        get_redis().delete(otp_key(bid_id))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=s.playwright_headless)
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            try:
                page.goto(ad["login_url"].format(**fmt))
                page.fill(ad["username"], creds["username"])
                page.fill(ad["password"], creds["password"])
                page.click(ad["login_submit"])
                page.wait_for_load_state("networkidle")
                page.goto(ad["tender_url"].format(**fmt))
                attached = []
                for slot, selector in ad["upload_slots"].items():
                    if slot in payload and page.locator(selector).count():
                        fname, data, _ = payload[slot]
                        page.set_input_files(selector, files=[{"name": fname, "mimeType": "application/pdf", "buffer": data}])
                        attached.append(slot)
                page.click(ad["final_submit"])
                page.wait_for_selector(ad["otp_input"], timeout=60_000)
                # ── pause cleanly for the human OTP/2FA ──
                shot = page.screenshot(full_page=True)
                otp_shot_key = storage.put(tenant_key(tenant_id, "bids", bid_id, "submission", "awaiting_otp.png"), shot, "image/png")
                bid.status = "AWAITING_OTP"
                bid.submission_receipt = {**(bid.submission_receipt or {}), "digital": {"adapter": key, "attached": attached, "awaiting_otp_since": datetime.now(timezone.utc).isoformat(),
                                                                                       "otp_screenshot_s3_key": otp_shot_key}}
                session.commit()
                publish(bid_channel(tenant_id, bid_id), "submission.awaiting_otp", {"attached": attached})
                deadline = time.time() + s.submission_otp_timeout_s
                otp = None
                while time.time() < deadline:
                    otp = get_redis().getdel(otp_key(bid_id))
                    if otp:
                        break
                    time.sleep(1)
                if not otp:
                    raise TimeoutError("No OTP entered before timeout — submission not completed")
                page.fill(ad["otp_input"], otp)
                page.click(ad["otp_submit"])
                page.wait_for_selector(ad["confirmation"], timeout=120_000)
                conf_text = page.inner_text(ad["confirmation"])
                m = re.search(ad["reference_regex"], conf_text)
                shot = page.screenshot(full_page=True)
            finally:
                browser.close()
        conf_key = storage.put(tenant_key(tenant_id, "bids", bid_id, "submission", "confirmation.png"), shot, "image/png")
        receipt = {
            "adapter": key, "attached": attached, "portal_reference": m.group(1) if m else None,
            "confirmation_text": conf_text[:500], "submitted_at": datetime.now(timezone.utc).isoformat(),
            "payload_sha256": payload_hash, "files": {k: {"name": v[0], "sha256": sha256_bytes(v[1]), **v[2]} for k, v in payload.items()},
            "confirmation_screenshot_s3_key": conf_key,
        }
        bid = session.get(BidPackage, uuid.UUID(bid_id))
        bid.submission_receipt = {**(bid.submission_receipt or {}), "digital": receipt}
        bid.status = "SUBMITTED"
        session.add(AuditEvent(tenant_id=bid.tenant_id, entity="bid", entity_id=bid_id, action="SUBMITTED_DIGITAL",
                               detail={"reference": receipt["portal_reference"], "payload_sha256": payload_hash}))
    publish(bid_channel(tenant_id, bid_id), "submission.completed", {"portal_reference": receipt["portal_reference"]})
    return receipt


def provide_otp(bid_id: str, otp: str) -> None:
    get_redis().set(otp_key(bid_id), otp.strip(), ex=600)


def zip_bytes(files: dict[str, bytes]) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in files.items():
            zf.writestr(n, d)
    return buf.getvalue()
