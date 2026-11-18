"""Harvest orchestration (every 4h) and the silent-addenda hash-watcher (every 6h)."""
from __future__ import annotations

import difflib
import io
import logging
import re
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from ..core import clamav
from ..core.crypto import sha256_bytes
from ..core.events import bid_channel, publish, tenant_channel
from ..db import system_session, tenant_session
from ..models import AuditEvent, BidPackage, Tender, Tenant
from . import preflight
from .harvesters.adapters import UA, make
from .harvesters.base import DOC_EXT, TenderRecord
from .harvesters.registry import load_portals, portals_by_key
from .service import apply_preflight, store_pack

log = logging.getLogger(__name__)
Fetcher = Callable[[str], bytes]


def http_fetch(url: str) -> bytes:
    with httpx.Client(timeout=120, headers={"User-Agent": UA}, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


# ── harvesting ──────────────────────────────────────────────────────────────────────────────
def harvest_all(only: list[str] | None = None) -> dict:
    summary: dict = {"portals": {}, "upserted": 0, "started_at": datetime.now(timezone.utc).isoformat()}
    records: list[TenderRecord] = []
    for cfg in load_portals():
        if only and cfg.key not in only:
            continue
        try:
            recs = make(cfg).fetch()
            records += recs
            summary["portals"][cfg.key] = {"ok": True, "count": len(recs)}
        except Exception as exc:  # noqa: BLE001 — one broken portal must not stop the run
            log.warning("harvest %s failed: %s", cfg.key, exc)
            summary["portals"][cfg.key] = {"ok": False, "error": str(exc)[:300]}
    summary["upserted"] = distribute(records)
    return summary


def _matches(tenant: Tenant, rec: TenderRecord) -> bool:
    keywords = [k.lower() for k in (tenant.sectors or []) + (tenant.subscribed_cpv_codes or []) if isinstance(k, str)]
    if not keywords:
        return True
    hay = " ".join(filter(None, [rec.title, rec.description, rec.category])).lower()
    return any(k in hay for k in keywords)


def distribute(records: list[TenderRecord]) -> int:
    with system_session() as s:
        tenants = list(s.scalars(select(Tenant)))
    n = 0
    for tenant in tenants:
        mine = [r for r in records if _matches(tenant, r)]
        if not mine:
            continue
        with tenant_session(tenant.id) as session:
            for r in mine:
                n += upsert_tender(session, tenant.id, r)
        publish(tenant_channel(tenant.id), "tenders.harvested", {"count": len(mine)})
    return n


def upsert_tender(session, tenant_id, r: TenderRecord) -> int:  # noqa: ANN001
    t = session.scalar(select(Tender).where(Tender.source_portal == r.source_portal, Tender.tender_reference == r.tender_reference))
    created = t is None
    if created:
        t = Tender(tenant_id=tenant_id, source_portal=r.source_portal, tender_reference=r.tender_reference,
                   source_files_sha256={}, mandatory_schedules=[], boq_line_items=[], addenda_history=[], preflight={})
        session.add(t)
    t.source_tier = r.source_tier
    t.title = r.title
    t.description = r.description
    t.source_url = r.source_url
    t.issuing_entity = r.issuing_entity
    t.sector_category = r.category
    t.province = r.province
    if r.closing:
        t.closing_timestamp = r.closing if r.closing.tzinfo else r.closing.replace(tzinfo=timezone.utc)
    hashes = dict(t.source_files_sha256 or {})
    for u in r.document_urls:
        hashes.setdefault(u, None)
    t.source_files_sha256 = hashes
    if r.briefing and r.briefing.get("compulsory"):
        t.has_compulsory_briefing = True
        if r.briefing.get("timestamp"):
            t.briefing_timestamp = datetime.fromisoformat(r.briefing["timestamp"])
        t.briefing_venue_or_link = r.briefing.get("venue_or_link") or t.briefing_venue_or_link
    return 1 if created else 0


def fetch_tender_pack(tenant_id: str, tender_id: str, fetch: Fetcher = http_fetch) -> dict:
    """Download every linked RFP/BoQ/SBD/addendum file, AV-scan, store and run pre-flight."""
    with tenant_session(tenant_id) as session:
        tender = session.get(Tender, uuid.UUID(tender_id))
        urls = [u for u in (tender.source_files_sha256 or {}) if u.startswith("http")]
        if not urls:
            return {"downloaded": 0, "reason": "no document links on listing"}
        buf = io.BytesIO()
        url_hashes = {}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, u in enumerate(urls):
                data = fetch(u)
                clamav.scan_bytes(data)
                url_hashes[u] = sha256_bytes(data)
                name = re.sub(r"[^\w.\-]", "_", u.rstrip("/").split("/")[-1].split("?")[0]) or f"file{i}"
                zf.writestr(f"{i:02d}_{name}", data)
        pack = buf.getvalue()
        store_pack(session, tender, f"{tender.tender_reference}.zip", pack)
        tender.source_files_sha256 = {**tender.source_files_sha256, **url_hashes}
        apply_preflight(session, tender, f"{tender.tender_reference}.zip", pack)
        return {"downloaded": len(urls)}


# ── silent addenda hash-watcher ────────────────────────────────────────────────────────────
def discover_links(page_url: str, fetch: Fetcher) -> list[str]:
    try:
        html = fetch(page_url).decode(errors="replace")
    except Exception:  # noqa: BLE001
        return []
    soup = BeautifulSoup(html, "lxml")
    return sorted({urljoin(page_url, a["href"]) for a in soup.find_all("a", href=True)
                   if a["href"].lower().split("?")[0].endswith(DOC_EXT)})


def semantic_diff(tender: Tender, new_name: str, new_bytes: bytes, old_text: str = "") -> dict:
    files = preflight.unpack(new_name, new_bytes)
    fields, cites = preflight.extract_rules(files)
    new_text = "\n".join(f.text for f in files)
    changed = {}
    if fields.get("closing_timestamp"):
        old = tender.closing_timestamp.isoformat() if tender.closing_timestamp else None
        if not old or fields["closing_timestamp"][:16] != old[:16]:
            changed["closing_timestamp"] = {"old": old, "new": fields["closing_timestamp"], "citation": cites.get("closing_timestamp")}
    if b := fields.get("briefing"):
        if b.get("timestamp") and (not tender.briefing_timestamp or b["timestamp"][:16] != tender.briefing_timestamp.isoformat()[:16]):
            changed["briefing"] = {"old": tender.briefing_timestamp.isoformat() if tender.briefing_timestamp else None, "new": b}
    if fields.get("functionality_threshold_pct") is not None and tender.functionality_threshold_pct is not None and \
            float(tender.functionality_threshold_pct) != fields["functionality_threshold_pct"]:
        changed["functionality_threshold_pct"] = {"old": float(tender.functionality_threshold_pct), "new": fields["functionality_threshold_pct"]}
    boq = preflight.extract_boq(files)
    if boq:
        old_q = {(l["line_no"], l["description"][:40]): l["quantity"] for l in tender.boq_line_items or []}
        deltas = [{"line_no": l["line_no"], "description": l["description"], "old_qty": old_q.get((l["line_no"], l["description"][:40])), "new_qty": l["quantity"]}
                  for l in boq if old_q.get((l["line_no"], l["description"][:40])) != l["quantity"]]
        if deltas:
            changed["boq"] = deltas[:200]
    diff = list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=0))
    added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++") and l[1:].strip()]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---") and l[1:].strip()]
    return {"changed_fields": changed, "lines_added": len(added), "lines_removed": len(removed),
            "sample_added": added[:15], "sample_removed": removed[:15]}


def watch_tenant(tenant_id: str, fetch: Fetcher = http_fetch) -> list[dict]:
    from ..bids.readiness import audit_bid

    detections = []
    with tenant_session(tenant_id) as session:
        active = list(session.scalars(select(BidPackage).where(BidPackage.status.notin_(["SUBMITTED", "WITHDRAWN"]))))
        for bid in active:
            tender = session.get(Tender, bid.tender_id)
            hashes = dict(tender.source_files_sha256 or {})
            urls = {u for u in hashes if u.startswith("http")}
            if tender.source_url and tender.source_url.startswith("http"):
                urls |= set(discover_links(tender.source_url, fetch))
            for url in sorted(urls):
                try:
                    data = fetch(url)
                except Exception as exc:  # noqa: BLE001
                    log.info("hashwatch fetch failed %s: %s", url, exc)
                    continue
                digest = sha256_bytes(data)
                old = hashes.get(url)
                if old == digest:
                    continue
                if url in hashes and old is None:
                    hashes[url] = digest  # advertised on listing but never downloaded — baseline only
                    continue
                clamav.scan_bytes(data)
                name = url.rstrip("/").split("/")[-1].split("?")[0] or "addendum"
                store_pack(session, tender, name, data, source_url=url)
                diff = semantic_diff(tender, name, data)
                entry = {
                    "id": uuid.uuid4().hex[:12], "detected_at": datetime.now(timezone.utc).isoformat(), "url": url,
                    "filename": name, "old_sha256": old, "new_sha256": digest, "kind": "NEW_FILE" if old is None else "CHANGED_FILE",
                    "diff": diff, "acknowledged": False,
                }
                tender.addenda_history = [*(tender.addenda_history or []), entry]
                hashes[url] = digest
                detections.append(entry)
                session.add(AuditEvent(tenant_id=tender.tenant_id, entity="tender", entity_id=str(tender.id),
                                       action="SILENT_ADDENDUM_DETECTED", detail={"url": url, "sha256": digest}))
            tender.source_files_sha256 = hashes
            session.flush()
            if detections:
                audit_bid(session, bid)
                publish(bid_channel(tenant_id, bid.id), "addendum.detected", {"tender_id": str(tender.id), "count": len(detections)})
    return detections


def watch_all(fetch: Fetcher = http_fetch) -> dict:
    with system_session() as s:
        ids = [str(t) for t in s.scalars(select(Tenant.id))]
    return {tid: len(watch_tenant(tid, fetch)) for tid in ids}


def portal_catalogue() -> list[dict]:
    return [{"key": p.key, "name": p.name, "tier": p.tier, "adapter": p.adapter, "list_url": p.list_url, "verified": p.verified}
            for p in portals_by_key().values()]
