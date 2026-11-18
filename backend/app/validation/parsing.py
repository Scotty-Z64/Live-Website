"""Deterministic field parsers shared by the statutory validators and the RFP analyzer."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}

_DATE_PATTERNS = [
    re.compile(r"\b(?P<y>20\d{2}|19\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})\b"),
    re.compile(r"\b(?P<d>\d{1,2})[-/.](?P<m>\d{1,2})[-/.](?P<y>20\d{2}|19\d{2})\b"),
    re.compile(r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>[A-Za-z]{3,9})\.?,?\s+(?P<y>20\d{2}|19\d{2})\b"),
    re.compile(r"\b(?P<mon>[A-Za-z]{3,9})\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>20\d{2}|19\d{2})\b"),
]

TIME_RE = re.compile(r"\b(?P<h>[01]?\d|2[0-3])[:hH](?P<m>[0-5]\d)\b")


def parse_date(s: str) -> date | None:
    for rx in _DATE_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        gd = m.groupdict()
        try:
            month = MONTHS.get(gd["mon"].lower()) if gd.get("mon") else int(gd["m"])
            if not month:
                continue
            return date(int(gd["y"]), int(month), int(gd["d"]))
        except (ValueError, TypeError):
            continue
    return None


def all_dates(s: str) -> list[date]:
    out: list[date] = []
    for rx in _DATE_PATTERNS:
        for m in rx.finditer(s):
            d = parse_date(m.group(0))
            if d and d not in out:
                out.append(d)
    return out


def parse_datetime(s: str) -> datetime | None:
    d = parse_date(s)
    if not d:
        return None
    t = TIME_RE.search(s)
    if t:
        return datetime(d.year, d.month, d.day, int(t.group("h")), int(t.group("m")))
    return datetime(d.year, d.month, d.day, 11, 0)


def labelled_value(text: str, *labels: str, max_len: int = 120) -> str | None:
    """Value following ``Label:`` on the same line; falls back to the next non-empty line only when
    no occurrence of any label carries a same-line value (avoids matching document titles)."""
    fallback = None
    for label in labels:
        rx = re.compile(rf"{label}[ \t]*[:\-]?[ \t]*(?P<v>[^\n]{{0,{max_len}}})(?:\n\s*(?P<n>[^\n]{{1,{max_len}}}))?", re.I)
        for m in rx.finditer(text):
            v = (m.group("v") or "").strip(" :\t")
            if v:
                return v
            if fallback is None and m.group("n"):
                fallback = m.group("n").strip()
    return fallback


def labelled_date(text: str, *labels: str) -> date | None:
    for label in labels:
        m = re.search(rf"{label}\s*[:\-]?\s*([^\n]{{0,60}})(?:\n([^\n]{{0,60}}))?", text, re.I)
        if m:
            d = parse_date(m.group(1) or "") or parse_date(m.group(2) or "")
            if d:
                return d
    return None


def parse_money(s: str) -> Decimal | None:
    m = re.search(r"R?\s*([\d][\d\s,]*(?:\.\d{1,2})?)\s*(million|m\b|bn|billion)?", s, re.I)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(",", "")
    try:
        val = Decimal(raw)
    except InvalidOperation:
        return None
    mult = (m.group(2) or "").lower()
    if mult in ("million", "m"):
        val *= 1_000_000
    elif mult in ("bn", "billion"):
        val *= 1_000_000_000
    return val


def parse_pct(s: str) -> Decimal | None:
    m = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%", s)
    return Decimal(m.group(1).replace(",", ".")) if m else None


# ── South African ID numbers ─────────────────────────────────────────────────────────────
SA_ID_RE = re.compile(r"(?<!\d)(\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{4}[01]\d{2})(?!\d)")


def luhn_ok(num: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def valid_sa_id(num: str) -> bool:
    if not re.fullmatch(r"\d{13}", num or ""):
        return False
    try:
        yy, mm, dd = int(num[:2]), int(num[2:4]), int(num[4:6])
        date(2000 + yy if yy < 30 else 1900 + yy, mm, dd)
    except ValueError:
        return False
    return num[10] in "01" and luhn_ok(num)


def find_sa_ids(text: str) -> list[str]:
    return [m for m in SA_ID_RE.findall(text) if valid_sa_id(m)]


def normalise_name(name: str) -> str:
    return " ".join(sorted(re.sub(r"[^A-Za-z ]", " ", name).upper().split()))
