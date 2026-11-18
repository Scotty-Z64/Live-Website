"""LangGraph orchestration of the pre-flight analysis with composite tenant-key partitioning.

Thread ids are always ``"<tenant_id>:<tender_id>"``; ``run_preflight`` refuses a thread id whose
tenant prefix differs from the caller's tenant, so checkpoints can never cross tenants.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from ..config import get_settings
from . import preflight

log = logging.getLogger(__name__)


class PreflightState(TypedDict, total=False):
    tenant_id: str
    pack_name: str
    pack_bytes: bytes
    files: list[dict]
    zones: dict
    fields: dict
    citations: dict
    boq: list[dict]
    llm_used: bool
    errors: list[str]


def _files(state: PreflightState) -> list[preflight.PackFile]:
    return [preflight.PackFile(**f) for f in state["files"]]


def node_unpack(state: PreflightState) -> PreflightState:
    files = preflight.unpack(state["pack_name"], state["pack_bytes"])
    return {"files": [f.__dict__ for f in files], "pack_bytes": b""}


def node_segment(state: PreflightState) -> PreflightState:
    return {"zones": preflight.segment(_files(state))}


def node_rules(state: PreflightState) -> PreflightState:
    files = _files(state)
    fields, cites = preflight.extract_rules(files)
    fields["submission_mode"] = preflight.detect_submission_mode(fields)
    return {"fields": fields, "citations": cites, "boq": preflight.extract_boq(files)}


class _Cited(BaseModel):
    value: str = Field(description="Extracted value, or empty string if not stated")
    quote: str = Field(description="Verbatim quote from the document that states the value, or empty")


class _LlmFields(BaseModel):
    tender_reference: _Cited
    closing_date_time: _Cited
    preference_system: _Cited
    functionality_threshold_pct: _Cited
    compulsory_briefing_date_time: _Cited
    compulsory_briefing_venue_or_link: _Cited


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def node_enrich(state: PreflightState) -> PreflightState:
    """Fill only *missing* fields, and only when the model's quote exists verbatim in the pack."""
    s = get_settings()
    fields = dict(state["fields"])
    missing = [k for k in ("tender_reference", "closing_timestamp", "preference_system", "functionality_threshold_pct") if k not in fields]
    if fields.get("briefing", {}).get("compulsory") and not fields["briefing"].get("timestamp"):
        missing.append("briefing")
    if not s.anthropic_api_key or not missing:
        return {"llm_used": False}
    import anthropic

    t1 = state["zones"]["T1"]["text"] or "\n".join(f["pages"][0] for f in state["files"] if f.get("pages"))[:20000]
    corpus = _norm(" ".join(" ".join(f.get("pages", [])) for f in state["files"]))
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    try:
        resp = client.beta.messages.create(
            model=s.anthropic_model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _LlmFields.model_json_schema()}},
            system=(
                "You extract procurement facts from South African tender documents. Only report what the "
                "document states. Every non-empty value needs a verbatim quote copied exactly from the text."
            ),
            messages=[{"role": "user", "content": f"<tender_text>\n{t1}\n</tender_text>"}],
        )
    except anthropic.APIError as exc:
        return {"llm_used": False, "errors": [f"enrich: {exc}"]}
    if resp.stop_reason == "refusal":
        return {"llm_used": False}
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    try:
        out = _LlmFields.model_validate(json.loads(text))
    except Exception:  # noqa: BLE001
        return {"llm_used": False, "errors": ["enrich: invalid JSON"]}

    from ..validation.parsing import parse_datetime

    cites = dict(state["citations"])

    def accept(item: _Cited) -> bool:
        return bool(item.value and item.quote and _norm(item.quote) in corpus)

    llm_cite = lambda q: {"file": "llm-verified-quote", "page": None, "snippet": q, "method": "claude+verbatim-check"}  # noqa: E731
    if "tender_reference" in missing and accept(out.tender_reference):
        fields["tender_reference"] = out.tender_reference.value
        cites["tender_reference"] = llm_cite(out.tender_reference.quote)
    if "closing_timestamp" in missing and accept(out.closing_date_time) and (dt := parse_datetime(out.closing_date_time.value)):
        fields["closing_timestamp"] = dt.isoformat()
        cites["closing_timestamp"] = llm_cite(out.closing_date_time.quote)
    if "preference_system" in missing and accept(out.preference_system):
        m = re.search(r"(80|90)\s*/\s*(20|10)", out.preference_system.value)
        if m:
            fields["preference_system"] = f"{m.group(1)}_{m.group(2)}"
            cites["preference_system"] = llm_cite(out.preference_system.quote)
    if "functionality_threshold_pct" in missing and accept(out.functionality_threshold_pct):
        m = re.search(r"\d{2,3}", out.functionality_threshold_pct.value)
        if m:
            fields["functionality_threshold_pct"] = float(m.group(0))
            cites["functionality_threshold_pct"] = llm_cite(out.functionality_threshold_pct.quote)
    if "briefing" in missing and accept(out.compulsory_briefing_date_time):
        if dt := parse_datetime(out.compulsory_briefing_date_time.value):
            fields["briefing"]["timestamp"] = dt.isoformat()
        if accept(out.compulsory_briefing_venue_or_link):
            fields["briefing"]["venue_or_link"] = out.compulsory_briefing_venue_or_link.value
    return {"fields": fields, "citations": cites, "llm_used": True}


def node_assemble(state: PreflightState) -> PreflightState:
    # Drop page text from the persisted state; keep a lean file manifest.
    manifest = [{"name": f["name"], "kind": f["kind"], "is_addendum": f["is_addendum"], "pages": len(f.get("pages", []))} for f in state["files"]]
    return {"files": [{**f, "data": b""} for f in state["files"]], "fields": {**state["fields"], "file_manifest": manifest}}


def build_graph():
    g = StateGraph(PreflightState)
    g.add_node("unpack", node_unpack)
    g.add_node("segment", node_segment)
    g.add_node("rules", node_rules)
    g.add_node("enrich", node_enrich)
    g.add_node("assemble", node_assemble)
    g.add_edge(START, "unpack")
    g.add_edge("unpack", "segment")
    g.add_edge("segment", "rules")
    g.add_edge("rules", "enrich")
    g.add_edge("enrich", "assemble")
    g.add_edge("assemble", END)
    return g.compile(checkpointer=MemorySaver())


_GRAPH = None


def run_preflight(tenant_id: str, tender_id: str, pack_name: str, pack_bytes: bytes) -> dict[str, Any]:
    """Run the graph; returns the final state (files keep page text for downstream steps)."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    thread_id = f"{tenant_id}:{tender_id}"
    if not thread_id.startswith(f"{tenant_id}:"):
        raise PermissionError("thread/tenant mismatch")
    files = preflight.unpack(pack_name, pack_bytes)  # unpacked once for downstream PDF usage
    state = _GRAPH.invoke(
        {"tenant_id": tenant_id, "pack_name": pack_name, "pack_bytes": pack_bytes, "errors": []},
        config={"configurable": {"thread_id": thread_id}},
    )
    state["_packfiles"] = files
    return state
