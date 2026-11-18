"""Pre-Flight RFP Understanding Phase (blueprint §6).

Steps, each a node in a LangGraph state machine (see ``graph.py``):
1. unpack     — recursively extract nested ZIPs, classify every file (RFP/BOQ/SBD/ADDENDUM/...).
2. segment    — four-zone segmentation: T1 procedures, T2 returnable schedules, C1/C2 pricing & BoQ,
                C3 scope of work.
3. rules      — deterministic disqualifier & sector-rule extraction with provenance citations.
4. enrich     — optional Claude pass that may only fill fields the rules missed AND must quote a
                verbatim snippet that is re-verified against the source text (strict provenance).
5. assemble   — final structured result persisted on ``tenders``.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

import fitz

from .. import ocr
from ..validation.parsing import parse_datetime, parse_pct

MAX_UNPACK_BYTES = 300 * 1024 * 1024
MAX_DEPTH = 4


@dataclass
class PackFile:
    name: str
    data: bytes
    kind: str = "OTHER"
    pages: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    is_addendum: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.pages)


# ── 1. unpack & classify ────────────────────────────────────────────────────────────────────
def unpack(name: str, data: bytes, depth: int = 0, budget: list[int] | None = None) -> list[PackFile]:
    budget = budget if budget is not None else [MAX_UNPACK_BYTES]
    if zipfile.is_zipfile(io.BytesIO(data)) and not name.lower().endswith((".xlsx", ".docx")):
        if depth >= MAX_DEPTH:
            return []
        out: list[PackFile] = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.filename.startswith("__MACOSX"):
                    continue
                p = PurePosixPath(info.filename)
                if ".." in p.parts or p.is_absolute():  # zip-slip guard
                    continue
                budget[0] -= info.file_size
                if budget[0] < 0:
                    raise ValueError("Tender pack exceeds 300 MB uncompressed (zip bomb guard)")
                out += unpack(f"{name}/{info.filename}" if depth else info.filename, zf.read(info), depth + 1, budget)
        return out
    return [classify_file(PackFile(name=name, data=data))]


def classify_file(f: PackFile) -> PackFile:
    lname = f.name.lower()
    if lname.endswith(".pdf"):
        try:
            f.pages, _ = ocr.text_layer(f.data)
        except Exception:  # noqa: BLE001
            f.pages = []
    elif lname.endswith((".xlsx", ".xlsm")):
        try:
            f.rows = ocr.xlsx_rows(f.data)
            f.pages = ["\n".join(" | ".join(r) for r in f.rows)]
        except Exception:  # noqa: BLE001
            f.pages = []
    elif lname.endswith(".docx"):
        f.pages = [_docx_text(f.data)]
    elif lname.endswith((".txt", ".csv")):
        f.pages = [f.data.decode(errors="replace")]
    title = (f.text[:400] + " " + lname).lower()
    head = (f.text[:3000] + " " + lname).lower()
    if re.search(r"addend(um|a)|errat(um|a)|amendment no|notice of (amendment|extension)", title):
        f.kind, f.is_addendum = "ADDENDUM", True
    elif lname.endswith((".xlsx", ".xlsm", ".csv")) or re.search(r"bill of quantit|\bboq\b|pricing schedule", title):
        f.kind = "BOQ"
    elif re.search(r"\bsbd\s?\d|\bmbd\s?\d|standard bidding document", title) and len(f.pages) <= 6:
        f.kind = "SBD"
    elif re.search(r"request for (proposal|quotation|bid)|invitation to (bid|tender)|tender (document|number)|bid number|\brfp\b|\brfq\b", head):
        f.kind = "RFP"
    elif lname.endswith(".pdf"):
        f.kind = "ANNEXURE"
    return f


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode(errors="replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    except Exception:  # noqa: BLE001
        return ""


# ── 2. four-zone segmentation ───────────────────────────────────────────────────────────────
ZONES = {
    "T1": (r"part\s*t\s?1\b|tendering procedures|tender data|conditions of tender", "Tendering Procedures & Briefing Rules"),
    "T2": (r"part\s*t\s?2\b|returnable (schedules|documents)|mandatory returnable", "Mandatory Returnable Schedules"),
    "C1C2": (r"part\s*c\s?[12]\b|pricing (data|instructions)|bill of quantities|pricing schedule", "Pricing Data & BoQ"),
    "C3": (r"part\s*c\s?3\b|scope of (work|services)|specifications?|terms of reference", "Technical Scope of Work"),
}


def segment(files: list[PackFile]) -> dict:
    """Assign each page of each document to a zone using heading markers; pages inherit the
    most recent zone. Returns {zone: {"label", "spans": [{file, page_from, page_to}], "text"}}."""
    zones: dict = {z: {"label": lbl, "spans": [], "text": ""} for z, (_, lbl) in ZONES.items()}
    for f in files:
        if f.kind == "BOQ":
            zones["C1C2"]["spans"].append({"file": f.name, "page_from": 1, "page_to": max(1, len(f.pages))})
            zones["C1C2"]["text"] += f.text + "\n"
            continue
        current = None
        for i, page in enumerate(f.pages):
            # A heading counts when it starts a line (avoid mid-sentence references).
            hits = []
            for z, (rx, _) in ZONES.items():
                m = re.search(rf"(?im)^\s*(?:\d+(?:\.\d+)*\s*)?(?:{rx})", page)
                if m:
                    hits.append((m.start(), z))
            for _, z in sorted(hits):
                current = z
                spans = zones[z]["spans"]
                if spans and spans[-1]["file"] == f.name and spans[-1]["page_to"] in (i, i + 1):
                    spans[-1]["page_to"] = i + 1
                else:
                    spans.append({"file": f.name, "page_from": i + 1, "page_to": i + 1})
            if current and not hits:
                spans = zones[current]["spans"]
                if spans and spans[-1]["file"] == f.name:
                    spans[-1]["page_to"] = i + 1
            if current:
                zones[current]["text"] += page + "\n"
    for z in zones.values():
        z["text"] = z["text"][:20000]
    return zones


# ── 3. disqualifier & sector rule extraction ────────────────────────────────────────────────
@dataclass
class Citation:
    file: str
    page: int
    snippet: str

    def to_json(self) -> dict:
        return {"file": self.file, "page": self.page, "snippet": self.snippet}


KIND_PRIORITY = {"RFP": 0, "ANNEXURE": 1, "OTHER": 2, "SBD": 3, "BOQ": 4, "ADDENDUM": 5}


def _ordered(files: list[PackFile], addenda_first: bool = False) -> list[PackFile]:
    def key(f: PackFile):  # noqa: ANN202
        if addenda_first and f.kind == "ADDENDUM":
            return (-1, f.name)
        return (KIND_PRIORITY.get(f.kind, 9), f.name)
    return sorted(files, key=key)


def _find(files: list[PackFile], pattern: str, flags=re.I | re.S, accept=None, addenda_first: bool = False):  # noqa: ANN001, ANN202
    """First match (optionally passing ``accept(match)``) with a provenance citation."""
    rx = re.compile(pattern, flags)
    for f in _ordered(files, addenda_first):
        for i, page in enumerate(f.pages):
            for m in rx.finditer(page):
                if accept and not accept(m):
                    continue
                a, b = max(0, m.start() - 60), min(len(page), m.end() + 60)
                return m, Citation(f.name, i + 1, " ".join(page[a:b].split()))
    return None


SCHEDULE_PATTERNS = [
    ("SBD 1", r"\bSBD\s?1\b(?!\d)"), ("SBD 3.1", r"\bSBD\s?3\.1\b"), ("SBD 3.2", r"\bSBD\s?3\.2\b"),
    ("SBD 4", r"\bSBD\s?4\b"), ("SBD 6.1", r"\bSBD\s?6\.1\b"), ("SBD 6.2", r"\bSBD\s?6\.2\b"),
    ("SBD 8", r"\bSBD\s?8\b"), ("SBD 9", r"\bSBD\s?9\b"),
    ("MBD 1", r"\bMBD\s?1\b(?!\d)"), ("MBD 4", r"\bMBD\s?4\b"), ("MBD 6.1", r"\bMBD\s?6\.1\b"),
    ("Tax Compliance Status PIN", r"tax compliance status|\bTCS\b"), ("CSD Report", r"central supplier database|\bCSD\b"),
    ("B-BBEE Certificate / Affidavit", r"b-?bbee (?:certificate|affidavit|status level)"),
    ("CIPC Registration", r"\bCIPC\b|company registration"), ("COIDA Letter of Good Standing", r"\bCOIDA\b|letter of good standing"),
    ("Certified ID copies of directors", r"certified (?:copies of )?id"), ("Company Profile", r"company profile"),
    ("Methodology / Project Plan", r"methodology|project plan|work plan"), ("Bank Confirmation Letter", r"bank (?:confirmation|rating)"),
    ("Municipal rates clearance", r"municipal (?:rates|account)|rates clearance"),
]

SECTOR_PLUGINS = {
    "CIDB": r"\bCIDB\b|construction industry development board",
    "PSIRA": r"\bPSIRA\b|private security",
    "WIREMAN": r"wireman'?s licen[cs]e|installation electrician|certificate of compliance|\bCoC\b",
    "SANS": r"\bSANS\s?\d{3,5}",
}


def extract_rules(files: list[PackFile]) -> tuple[dict, dict[str, dict]]:
    """Returns (fields, citations) — every populated field has a citation."""
    docs = [f for f in files if f.kind != "BOQ"] or files
    fields: dict = {}
    cites: dict[str, dict] = {}

    def put(key: str, value, cite: Citation) -> None:
        fields[key] = value
        cites[key] = cite.to_json()

    if r := _find(docs, r"(?:bid|tender|rfq|rfp|quotation)[ \t]*(?:no\.?|number|reference|ref\.?)[ \t]*[:\-]?[ \t]*([A-Za-z0-9][A-Za-z0-9/\-._]{3,40})",
                  accept=lambda m: bool(re.search(r"\d", m.group(1)))):
        put("tender_reference", r[0].group(1).strip(" .-"), r[1])
    if r := _find(docs, r"closing (?:date|time)(?: and time)?[ \t]*[:\-]?[ \t]*([^\n]{6,60})",
                  accept=lambda m: parse_datetime(m.group(1)) is not None, addenda_first=True):
        put("closing_timestamp", parse_datetime(r[0].group(1)).isoformat(), r[1])
    if r := _find(docs, r"(?:description|project (?:name|title)|bid description|tender description)[ \t]*[:\-][ \t]*([^\n]{8,200})"):
        put("title", r[0].group(1).strip(), r[1])
    entity_rx = (r"^[ \t]*((?:city|municipality|metropolitan|department|dept\.?|provincial|national treasury|[a-z ]*seta\b|"
                 r"[a-z &]*(?:soc|holdings|agency|board|water|authority|council|ltd))[^\n]{0,80}?)(?:[ \t]+[—\-·|][ \t]+|\n)")
    if r := _find([f for f in docs if f.kind == "RFP"] or docs, entity_rx, flags=re.I | re.M):
        put("issuing_entity", r[0].group(1).strip(" -—·|"), r[1])
    if r := _find(docs, r"\b(80\s?/\s?20|90\s?/\s?10)\b(?:\s*preference)?"):
        put("preference_system", r[0].group(1).replace(" ", "").replace("/", "_"), r[1])
    if r := _find(docs, r"(?:minimum|threshold)[^\n.]{0,80}?(\d{2,3})\s*(?:%|percent|points)[^\n.]{0,60}?functionality|functionality[^\n.]{0,120}?(?:minimum|threshold|at least)[^\n.]{0,40}?(\d{2,3})\s*(?:%|percent|points)"):
        put("functionality_threshold_pct", float(r[0].group(1) or r[0].group(2)), r[1])
    if r := _find(docs, r"local (?:content|production)[^\n.]{0,120}?(\d{2,3})\s*%"):
        put("local_content_threshold_pct", float(r[0].group(1)), r[1])
    if r := _find(docs, r"CIDB[^\n]{0,80}?(?:grad(?:e|ing)\s*(?:of\s*)?)?\b([1-9])\s?(CE|GB|ME|EB|EP|SB|SC|SD|SE|SF|SG|SH|SI|SJ|SK|SL|SM|SN|SO|SQ)\b"):
        put("cidb_min_grade", {"grade": int(r[0].group(1)), "class": r[0].group(2).upper()}, r[1])
    if r := _find(docs, r"(?:submit|upload|submitted|submission)[^\n.]{0,80}?(?:e-?tender(?:s)? portal|electronic(?:ally)?|online portal|via the portal)"):
        put("digital_submission", True, r[1])
    if r := _find(docs, r"(?:tender box|deposited in|hand[- ]delivered to|physical address for submission)[^\n]{0,20}?[:\-]?\s*([^\n]{8,160})"):
        put("physical_tender_box_address", r[0].group(1).strip(), r[1])
    if r := _find(docs, r"(https?://\S*(?:etenders|portal|supplier|tender)\S*)"):
        put("portal_url", r[0].group(1).rstrip(").,"), r[1])
    if r := _find(docs, r"(initial(?:led)? (?:on )?(?:each|every) page)"):
        put("initial_every_page", True, r[1])
    if r := _find(docs, r"(?:validity period|valid for a period of|bid validity)[^\n.]{0,40}?(\d{2,3})\s*days"):
        put("validity_days", int(r[0].group(1)), r[1])
    if r := _find(docs, r"(?:max(?:imum)? (?:file )?size|not exceed)[^\n.]{0,40}?(\d{1,3})\s*MB"):
        put("portal_max_mb", int(r[0].group(1)), r[1])

    # Mandatory returnable schedules (T2) — cite each.
    sched = []
    for label, rx in SCHEDULE_PATTERNS:
        if r := _find(docs, rx):
            sched.append({"name": label, "citation": r[1].to_json()})
    fields["mandatory_schedules"] = sched

    plugins = []
    for plug, rx in SECTOR_PLUGINS.items():
        if r := _find(docs, rx):
            entry = {"plugin": plug, "citation": r[1].to_json()}
            if plug == "SANS":
                entry["standards"] = sorted(set(re.findall(r"SANS\s?\d{3,5}(?:-\d+)?", " ".join(f.text for f in docs))))[:20]
            plugins.append(entry)
    fields["sector_plugins"] = plugins

    briefing = extract_briefing(docs)
    if briefing:
        fields["briefing"] = briefing
    return fields, cites


def extract_briefing(docs: list[PackFile]) -> dict | None:
    r = _find(docs, r"(compulsory|mandatory|non-compulsory|optional)\s+(?:clarification\s+|site\s+|virtual\s+|online\s+)?(?:briefing|site (?:visit|meeting)|clarification meeting)")
    if not r:
        return None
    m, cite = r
    compulsory = m.group(1).lower() in ("compulsory", "mandatory")
    f = next(x for x in docs if x.name == cite.file)
    page = f.pages[cite.page - 1]
    window = page[m.start(): m.start() + 900]
    when = None
    dm = re.search(r"(?:date|on)\s*[:\-]?\s*([^\n]{6,60})", window, re.I)
    if dm:
        when = parse_datetime(dm.group(1) + " " + window[dm.end(): dm.end() + 40])
    tm = re.search(r"time\s*[:\-]?\s*(\d{1,2}[:hH]\d{2})", window, re.I)
    if when and tm:
        h, mi = re.split(r"[:hH]", tm.group(1))
        when = when.replace(hour=int(h), minute=int(mi))
    venue = None
    vm = re.search(r"(?:venue|location|address|place)\s*[:\-]?\s*([^\n]{6,200})", window, re.I)
    if vm:
        venue = vm.group(1).strip()
    link = re.search(r"https?://\S*(?:teams\.microsoft|zoom\.us|meet\.google|webex)\S*", window, re.I)
    reg = re.search(r"(?:register|rsvp|booking|confirm attendance)[^\n]{0,80}?(https?://\S+)", window, re.I) or re.search(
        r"(?:register|rsvp|booking)[^\n]{0,80}?([\w.+-]+@[\w-]+\.[\w.]+)", window, re.I)
    return {
        "compulsory": compulsory,
        "timestamp": when.isoformat() if when else None,
        "venue_or_link": (link.group(0).rstrip(").,") if link else venue),
        "venue": venue,
        "online_link": link.group(0).rstrip(").,") if link else None,
        "registration_url": (reg.group(1).rstrip(").,") if reg else None),
        "citation": cite.to_json(),
    }


# ── BoQ extraction ──────────────────────────────────────────────────────────────────────────
BOQ_HEADER = {
    "item": r"^(item|no\.?|item no\.?|#)$",
    "description": r"description|item description|particulars",
    "unit": r"^(unit|uom|unit of measure)$",
    "quantity": r"^(qty|quantity|quant\.?)$",
}


def extract_boq(files: list[PackFile]) -> list[dict]:
    lines: list[dict] = []
    for f in files:
        if f.kind != "BOQ" or not f.rows:
            continue
        header_idx, cols = None, {}
        for i, row in enumerate(f.rows[:30]):
            found = {}
            for key, rx in BOQ_HEADER.items():
                for j, cell in enumerate(row):
                    if re.search(rx, cell.strip(), re.I):
                        found.setdefault(key, j)
            if "description" in found and "quantity" in found:
                header_idx, cols = i, found
                break
        if header_idx is None:
            continue
        for row in f.rows[header_idx + 1:]:
            def cell(k):
                j = cols.get(k)
                return row[j].strip() if j is not None and j < len(row) else ""
            desc = cell("description")
            qty_raw = cell("quantity").replace(",", "")
            try:
                qty = float(qty_raw)
            except ValueError:
                continue
            if not desc:
                continue
            lines.append({
                "line_no": cell("item") or str(len(lines) + 1),
                "description": desc,
                "unit": cell("unit") or "each",
                "quantity": qty,
                "source": {"file": f.name},
            })
    return lines


def detect_submission_mode(fields: dict) -> str:
    digital = bool(fields.get("digital_submission") or fields.get("portal_url"))
    physical = bool(fields.get("physical_tender_box_address"))
    if digital and physical:
        return "HYBRID"
    return "DIGITAL_PORTAL" if digital else "MANUAL_HARDCOPY"


def file_page_count(data: bytes) -> int | None:
    try:
        return fitz.open(stream=data, filetype="pdf").page_count
    except Exception:  # noqa: BLE001
        return None


def summarise_for_ui(fields: dict, zones: dict) -> dict:
    return {
        "zones": {k: {"label": v["label"], "spans": v["spans"], "chars": len(v["text"])} for k, v in zones.items()},
        "fields": fields,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def pct_or_none(s: str | None) -> float | None:
    p = parse_pct(s or "")
    return float(p) if p is not None else None
