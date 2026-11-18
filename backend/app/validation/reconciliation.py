"""Cross-entity reconciliation of directors / ownership across CIPC, CSD and B-BBEE records."""
from __future__ import annotations

from ..models import DocCategory


def reconcile(sources: dict[str, list[dict]]) -> list[dict]:
    """``sources``: {doc_category: [{sa_id_hash, full_name, shareholding_pct}]}

    Returns blocker issues. CIPC is the source of truth for the director set.
    """
    issues: list[dict] = []
    cipc = sources.get(DocCategory.CIPC_COR14_3.value) or []
    if not cipc:
        return issues
    cipc_by_hash = {d["sa_id_hash"]: d for d in cipc}
    cipc_set = set(cipc_by_hash)

    for cat in (DocCategory.CSD_REPORT.value, DocCategory.BBBEE_AFFIDAVIT.value):
        other = sources.get(cat) or []
        if not other:
            continue
        other_set = {d["sa_id_hash"] for d in other}
        missing = cipc_set - other_set
        extra = other_set - cipc_set
        if cat == DocCategory.CSD_REPORT.value and missing:
            names = ", ".join(cipc_by_hash[h]["full_name"] for h in missing)
            issues.append({"code": "DIRECTOR_MISMATCH", "message": f"Directors on CIPC but not on CSD: {names}", "sources": ["CIPC_COR14_3", cat]})
        if extra:
            names = ", ".join(d["full_name"] for d in other if d["sa_id_hash"] in extra)
            issues.append({"code": "DIRECTOR_MISMATCH", "message": f"{cat} lists persons who are not CIPC directors: {names}", "sources": ["CIPC_COR14_3", cat]})
        for d in other:
            ref = cipc_by_hash.get(d["sa_id_hash"])
            if ref and ref.get("shareholding_pct") is not None and d.get("shareholding_pct") is not None:
                if abs(float(ref["shareholding_pct"]) - float(d["shareholding_pct"])) > 0.5:
                    issues.append({
                        "code": "OWNERSHIP_MISMATCH",
                        "message": f"{ref['full_name']}: {ref['shareholding_pct']}% on CIPC vs {d['shareholding_pct']}% on {cat}",
                        "sources": ["CIPC_COR14_3", cat],
                    })
    pcts = [float(d["shareholding_pct"]) for d in cipc if d.get("shareholding_pct") is not None]
    if pcts and len(pcts) == len(cipc) and abs(sum(pcts) - 100) > 1:
        issues.append({"code": "OWNERSHIP_MISMATCH", "message": f"CIPC shareholding totals {sum(pcts):.1f}% (expected 100%)", "sources": ["CIPC_COR14_3"]})
    return issues
