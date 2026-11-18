"""Harvester contracts and the generic listing-page parser shared by HTML/Playwright adapters."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ...validation.parsing import all_dates, parse_datetime

DOC_EXT = (".pdf", ".zip", ".docx", ".doc", ".xlsx", ".xls", ".rar")
REF_RE = re.compile(r"\b((?:[A-Z]{2,10}[\s/\-_]?)+\d{1,6}(?:[/\-_.][A-Z0-9]{1,10}){0,5})\b")


@dataclass
class TenderRecord:
    source_portal: str
    source_tier: str
    tender_reference: str
    title: str
    source_url: str
    issuing_entity: str | None = None
    description: str | None = None
    category: str | None = None
    province: str | None = None
    closing: datetime | None = None
    document_urls: list[str] = field(default_factory=list)
    briefing: dict | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class PortalConfig:
    key: str
    name: str
    tier: str  # NATIONAL_PROVINCIAL | MUNICIPAL | SOE | SETA | PRIVATE
    list_url: str
    adapter: str  # ocds | html | playwright
    issuing_entity: str
    province: str | None = None
    verified: bool = False  # selectors/URL calibrated against the live site
    notes: str = ""


class Harvester:
    def __init__(self, cfg: PortalConfig):
        self.cfg = cfg

    def fetch(self) -> list[TenderRecord]:  # pragma: no cover - interface
        raise NotImplementedError


def parse_listing_html(html: str, cfg: PortalConfig, base_url: str | None = None) -> list[TenderRecord]:
    """Heuristic tender-table parser: a row/card is a tender if it carries a reference-like token
    AND (a closing date OR a document link)."""
    soup = BeautifulSoup(html, "lxml")
    base = base_url or cfg.list_url
    blocks = soup.select("tr") or []
    blocks += soup.select("li, article, div.tender, div[class*=tender], div[class*=Tender], div.card")
    out: dict[str, TenderRecord] = {}
    for blk in blocks:
        text = " ".join(blk.get_text(" ", strip=True).split())
        if len(text) < 15 or len(text) > 3000:
            continue
        if blk.name != "tr" and blk.find("tr"):
            continue
        links = [urljoin(base, a["href"]) for a in blk.find_all("a", href=True)]
        docs = [u for u in links if u.lower().split("?")[0].endswith(DOC_EXT)]
        dates = all_dates(text)
        ref_m = None
        for m in REF_RE.finditer(text):
            tok = m.group(1)
            if re.search(r"\d", tok) and not re.fullmatch(r"(?:SBD|MBD|SANS|CIDB|PSIRA)\s?[\d.]+", tok):
                ref_m = m
                break
        if not ref_m or not (dates or docs):
            continue
        ref = ref_m.group(1).strip()
        cells = [c.get_text(" ", strip=True) for c in blk.find_all(["td", "h2", "h3", "h4", "p", "span"])]
        title = max(cells, key=len) if cells else text[:200]
        closing = None
        cm = re.search(r"clos\w*[^0-9A-Za-z]{0,10}([^|]{6,40})", text, re.I)
        if cm:
            closing = parse_datetime(cm.group(1))
        if not closing and dates:
            d = max(dates)
            closing = datetime(d.year, d.month, d.day, 11, 0)
        briefing = None
        if re.search(r"(compulsory|mandatory)\s+(?:site\s+)?(?:briefing|site (?:visit|meeting)|clarification)", text, re.I):
            bm = re.search(r"briefing[^|]{0,120}", text, re.I)
            briefing = {"compulsory": True, "text": bm.group(0) if bm else None}
        out.setdefault(ref, TenderRecord(
            source_portal=cfg.key, source_tier=cfg.tier, tender_reference=ref, title=title[:500],
            source_url=next((u for u in links if not u.lower().endswith(DOC_EXT)), base), issuing_entity=cfg.issuing_entity,
            province=cfg.province, closing=closing, document_urls=docs, briefing=briefing, raw={"row_text": text[:1000]},
        ))
    return list(out.values())
