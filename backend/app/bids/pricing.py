"""BoQ ↔ catalog matching (pgvector cosine) and the 15% VAT / margin calculator."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import CatalogItem
from .embeddings import embed_many

MATCH_MIN_SIMILARITY = 0.35
CENT = Decimal("0.01")


def money(x) -> Decimal:  # noqa: ANN001
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def match_boq(session: Session, lines: list[dict], top_k: int = 3) -> list[dict]:
    if not lines:
        return []
    vectors = embed_many([f"{l['description']} {l.get('unit', '')}" for l in lines])
    out = []
    for line, vec in zip(lines, vectors):
        dist = CatalogItem.embedding.cosine_distance(vec)
        rows = session.execute(
            select(CatalogItem, dist.label("d")).where(CatalogItem.embedding.is_not(None)).order_by(dist).limit(top_k)
        ).all()
        cands = [{"catalog_item_id": str(ci.id), "sku_code": ci.sku_code, "item_name": ci.item_name,
                  "unit_price_excl_vat": float(ci.unit_price_excl_vat), "unit_of_measure": ci.unit_of_measure,
                  "local_content_pct": float(ci.local_content_pct) if ci.local_content_pct is not None else None,
                  "similarity": round(1 - float(d), 4)} for ci, d in rows]
        best = cands[0] if cands and cands[0]["similarity"] >= MATCH_MIN_SIMILARITY else None
        out.append({**line, "candidates": cands, "match": best})
    return out


def compute(lines: list[dict], margin_pct: float, vat_rate: float | None = None) -> dict:
    """``lines``: [{line_no, description, unit, quantity, unit_cost (excl VAT) | None, local_content_pct}]"""
    vat_rate = get_settings().vat_rate if vat_rate is None else vat_rate
    margin = Decimal(str(margin_pct)) / 100
    priced, subtotal, lc_weighted, lc_base, unpriced = [], Decimal(0), Decimal(0), Decimal(0), []
    for l in lines:
        qty = Decimal(str(l.get("quantity") or 0))
        cost = l.get("unit_cost")
        if cost is None:
            unpriced.append(l.get("line_no"))
            priced.append({**l, "unit_price": None, "line_total": None})
            continue
        unit_price = money(Decimal(str(cost)) * (1 + margin))
        total = money(unit_price * qty)
        subtotal += total
        if l.get("local_content_pct") is not None:
            lc_weighted += total * Decimal(str(l["local_content_pct"]))
            lc_base += total
        priced.append({**l, "unit_price": float(unit_price), "line_total": float(total)})
    subtotal = money(subtotal)
    vat = money(subtotal * Decimal(str(vat_rate)))
    return {
        "margin_pct": float(margin_pct),
        "vat_rate": vat_rate,
        "lines": priced,
        "unpriced_lines": unpriced,
        "subtotal_excl_vat": float(subtotal),
        "vat": float(vat),
        "total_incl_vat": float(subtotal + vat),
        "local_content_weighted_pct": float((lc_weighted / lc_base).quantize(CENT)) if lc_base else None,
    }


# PPPFA (2017 table; also default in 2022 regulations where the organ of state uses B-BBEE as its specific goal)
BBBEE_POINTS = {
    "80_20": {1: 20, 2: 18, 3: 14, 4: 12, 5: 8, 6: 6, 7: 4, 8: 2, None: 0},
    "90_10": {1: 10, 2: 9, 3: 6, 4: 5, 5: 4, 6: 3, 7: 2, 8: 1, None: 0},
}


def preference_points(system: str | None, level: int | None) -> int:
    return BBBEE_POINTS.get(system or "80_20", BBBEE_POINTS["80_20"]).get(level, 0)
