"""Concrete harvesters: eTenders OCDS REST API, plain-HTTP listing pages, Playwright listing pages."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx

from ...config import get_settings
from ...validation.parsing import parse_datetime
from .base import Harvester, PortalConfig, TenderRecord, parse_listing_html

log = logging.getLogger(__name__)
UA = "TenderSyncSA-Harvester/1.0 (+procurement monitoring)"


class OcdsHarvester(Harvester):
    """National Treasury eTenders OCDS API (Open Contracting Data Standard releases)."""

    page_size = 100
    max_pages = 20

    def fetch(self, date_from: date | None = None, date_to: date | None = None) -> list[TenderRecord]:
        date_to = date_to or date.today()
        date_from = date_from or date_to - timedelta(days=14)
        out: list[TenderRecord] = []
        with httpx.Client(timeout=60, headers={"User-Agent": UA}) as client:
            for page in range(1, self.max_pages + 1):
                r = client.get(self.cfg.list_url, params={
                    "PageNumber": page, "PageSize": self.page_size,
                    "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(),
                })
                r.raise_for_status()
                releases = r.json().get("releases", [])
                out += [rec for rel in releases if (rec := self.parse_release(rel))]
                if len(releases) < self.page_size:
                    break
        return out

    def parse_release(self, rel: dict) -> TenderRecord | None:
        t = rel.get("tender") or {}
        if (t.get("status") or "").lower() not in ("active", "open", "planned", ""):
            return None
        ref = t.get("id") or t.get("title") or rel.get("ocid")
        if not ref:
            return None
        period = t.get("tenderPeriod") or {}
        closing = None
        if period.get("endDate"):
            try:
                closing = datetime.fromisoformat(period["endDate"].replace("Z", "+00:00"))
            except ValueError:
                closing = parse_datetime(period["endDate"])
        docs = [d.get("url") for d in t.get("documents", []) if d.get("url")]
        b = t.get("briefingSession") or {}
        briefing = None
        if b.get("isSession") or b.get("compulsory"):
            when = None
            if b.get("date"):
                try:
                    when = datetime.fromisoformat(str(b["date"]).replace("Z", "+00:00"))
                except ValueError:
                    when = parse_datetime(str(b["date"]))
            briefing = {"compulsory": bool(b.get("compulsory")), "timestamp": when.isoformat() if when else None, "venue_or_link": b.get("venue")}
        entity = (t.get("procuringEntity") or {}).get("name") or ((rel.get("buyer") or {}).get("name"))
        return TenderRecord(
            source_portal=self.cfg.key, source_tier=self.cfg.tier, tender_reference=str(ref)[:250],
            title=(t.get("title") or "")[:500], description=t.get("description"),
            source_url=f"https://www.etenders.gov.za/Home/opportunities?id={rel.get('ocid', '')}",
            issuing_entity=entity, category=t.get("mainProcurementCategory") or t.get("category"),
            province=t.get("province"), closing=closing, document_urls=docs, briefing=briefing,
            raw={"ocid": rel.get("ocid")},
        )


class HtmlListingHarvester(Harvester):
    def fetch(self) -> list[TenderRecord]:
        with httpx.Client(timeout=60, headers={"User-Agent": UA}, follow_redirects=True) as client:
            r = client.get(self.cfg.list_url)
            r.raise_for_status()
            return parse_listing_html(r.text, self.cfg, str(r.url))


class PlaywrightListingHarvester(Harvester):
    """For JavaScript-rendered portals (SOE / municipal / private supplier portals)."""

    def fetch(self) -> list[TenderRecord]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=get_settings().playwright_headless)
            try:
                page = browser.new_page(user_agent=UA)
                page.goto(self.cfg.list_url, wait_until="networkidle", timeout=90_000)
                # Expand paginated tables where a "show all" / page-size control exists.
                for sel in ("select[name$='length']", "select[aria-controls]"):
                    if page.locator(sel).count():
                        try:
                            page.select_option(sel, index=page.locator(f"{sel} option").count() - 1)
                            page.wait_for_load_state("networkidle")
                        except Exception:  # noqa: BLE001
                            pass
                html = page.content()
                url = page.url
            finally:
                browser.close()
        return parse_listing_html(html, self.cfg, url)


ADAPTERS = {"ocds": OcdsHarvester, "html": HtmlListingHarvester, "playwright": PlaywrightListingHarvester}


def make(cfg: PortalConfig) -> Harvester:
    return ADAPTERS[cfg.adapter](cfg)
