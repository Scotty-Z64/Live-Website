"""REST API (all routes are tenant-scoped through the JWT principal + PostgreSQL RLS)."""
from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from ..bids import service as bids
from ..bids.embeddings import embed_many, provider
from ..bids.readiness import audit_bid, has_blockers
from ..config import get_settings
from ..core.crypto import decrypt_pii, encrypt_pii, mask_id
from ..core.storage import get_storage, verify_local
from ..db import system_session, tenant_session
from ..models import (
    AuditEvent,
    BidPackage,
    CatalogItem,
    Director,
    DocCategory,
    SubmissionMode,
    Tender,
    Tenant,
    User,
    VaultDocument,
)
from ..pdf import letters
from ..security import Principal, current_principal, hash_password, issue_token, verify_password
from ..tenders import service as tenders
from ..tenders.harvest import portal_catalogue
from ..validation.pipeline import UploadRejected, ingest_upload
from . import serializers as ser

router = APIRouter(prefix="/api")


def _dispatch(task, *args):  # noqa: ANN001, ANN202
    return task.delay(*args)


# ── auth & onboarding ───────────────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    legal_name: str = Field(min_length=2)
    trading_name: str | None = None
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    cipc_reg_number: str | None = None
    csd_maaa_number: str | None = None
    vat_number: str | None = None
    annual_turnover_zar: float | None = None
    black_ownership_pct: float | None = None
    black_women_ownership_pct: float | None = None
    physical_address: str | None = None
    contact_phone: str | None = None
    sectors: list[str] = []


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@router.post("/auth/register")
def register(body: RegisterIn) -> dict:
    with system_session() as s:
        if s.scalar(select(User).where(func.lower(User.email) == body.email.lower())):
            raise HTTPException(409, "An account with this e-mail already exists")
        t = Tenant(legal_name=body.legal_name.strip(), trading_name=body.trading_name, cipc_reg_number=body.cipc_reg_number or None,
                   csd_maaa_number=body.csd_maaa_number or None, vat_number=body.vat_number,
                   annual_turnover_zar=Decimal(str(body.annual_turnover_zar)) if body.annual_turnover_zar is not None else None,
                   black_ownership_pct=Decimal(str(body.black_ownership_pct)) if body.black_ownership_pct is not None else None,
                   black_women_ownership_pct=Decimal(str(body.black_women_ownership_pct)) if body.black_women_ownership_pct is not None else None,
                   physical_address=body.physical_address, contact_person=body.full_name, contact_email=body.email,
                   contact_phone=body.contact_phone, sectors=body.sectors, subscribed_cpv_codes=[])
        s.add(t)
        s.flush()
        u = User(tenant_id=t.id, email=body.email.lower(), full_name=body.full_name, password_hash=hash_password(body.password))
        s.add(u)
        s.flush()
        return {"token": issue_token(u.id, t.id, u.email), "tenant_id": str(t.id)}


@router.post("/auth/login")
def login(body: LoginIn) -> dict:
    with system_session() as s:
        u = s.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
        if not u or not verify_password(body.password, u.password_hash):
            raise HTTPException(401, "Incorrect e-mail or password")
        return {"token": issue_token(u.id, u.tenant_id, u.email), "tenant_id": str(u.tenant_id)}


TENANT_FIELDS = ["legal_name", "trading_name", "cipc_reg_number", "csd_maaa_number", "vat_number", "annual_turnover_zar", "bee_level",
                 "black_ownership_pct", "black_women_ownership_pct", "subscribed_cpv_codes", "physical_address", "contact_person",
                 "contact_email", "contact_phone", "sectors"]


def _tenant_out(t: Tenant) -> dict:
    out = {k: getattr(t, k) for k in TENANT_FIELDS}
    for k, v in out.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
    return {"id": str(t.id), **out, "created_at": ser.iso(t.created_at)}


@router.get("/tenant")
def get_tenant(p: Principal = Depends(current_principal)) -> dict:
    with system_session() as s:
        return _tenant_out(s.get(Tenant, p.tenant_id))


@router.patch("/tenant")
def patch_tenant(body: dict, p: Principal = Depends(current_principal)) -> dict:
    with system_session() as s:
        t = s.get(Tenant, p.tenant_id)
        for k, v in body.items():
            if k in TENANT_FIELDS and k != "bee_level":
                if k in ("annual_turnover_zar", "black_ownership_pct", "black_women_ownership_pct") and v is not None:
                    v = Decimal(str(v))
                setattr(t, k, v)
        s.flush()
        out = _tenant_out(t)
    from ..bids.readiness import reaudit_open_bids

    reaudit_open_bids(str(p.tenant_id))
    return out


@router.get("/directors")
def directors(p: Principal = Depends(current_principal)) -> list[dict]:
    with tenant_session(p.tenant_id) as s:
        return [{"id": str(d.id), "full_name": d.full_name, "sa_id_masked": mask_id(decrypt_pii(d.sa_id_encrypted, "sa_id")),
                 "shareholding_pct": float(d.shareholding_pct) if d.shareholding_pct is not None else None,
                 "is_historically_disadvantaged": d.is_historically_disadvantaged} for d in s.scalars(select(Director).order_by(Director.full_name))]


# ── vault ───────────────────────────────────────────────────────────────────────────────────
@router.get("/vault")
def vault(p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        docs = list(s.scalars(select(VaultDocument).order_by(VaultDocument.uploaded_at.desc())))
        slots = {c.value: [] for c in DocCategory}
        unclassified = []
        for d in docs:
            if d.extracted_json.get("kind") == "BRIEFING_ATTENDANCE_CERT":
                continue
            (slots[d.doc_category.value] if d.doc_category else unclassified).append(ser.vault_doc(d))
        return {"slots": slots, "unclassified": unclassified}


async def _read(upload: UploadFile) -> bytes:
    data = await upload.read()
    if not data:
        raise HTTPException(400, "Empty file")
    return data


@router.post("/vault/upload")
async def vault_upload(file: UploadFile = File(...), doc_category: str | None = Form(default=None), p: Principal = Depends(current_principal)) -> dict:
    from ..worker import process_vault_document

    cat = DocCategory(doc_category) if doc_category else None
    data = await _read(file)
    try:
        with tenant_session(p.tenant_id) as s:
            doc, created = ingest_upload(s, p.tenant_id, file.filename or "upload", data, cat)
            doc_id = str(doc.id)
            needs_processing = created or doc.processing_state == "QUEUED"
    except UploadRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    if needs_processing:
        _dispatch(process_vault_document, str(p.tenant_id), doc_id)
    with tenant_session(p.tenant_id) as s:
        return {"document": ser.vault_doc(s.get(VaultDocument, uuid.UUID(doc_id))), "duplicate": not created}


@router.post("/vault/bulk")
async def vault_bulk(files: list[UploadFile] = File(...), p: Principal = Depends(current_principal)) -> dict:
    results = []
    for f in files:
        try:
            r = await vault_upload(file=f, doc_category=None, p=p)
            results.append({"filename": f.filename, "ok": True, **r})
        except HTTPException as exc:
            results.append({"filename": f.filename, "ok": False, "error": exc.detail})
    return {"results": results}


@router.post("/vault/{doc_id}/category")
def set_category(doc_id: uuid.UUID, body: dict, p: Principal = Depends(current_principal)) -> dict:
    from ..worker import process_vault_document

    with tenant_session(p.tenant_id) as s:
        d = s.get(VaultDocument, doc_id) or _404()
        d.doc_category = DocCategory(body["doc_category"])
        d.processing_state = "QUEUED"
    _dispatch(process_vault_document, str(p.tenant_id), str(doc_id))
    with tenant_session(p.tenant_id) as s:
        return ser.vault_doc(s.get(VaultDocument, doc_id))


@router.get("/vault/templates/bbbee-affidavit")
def affidavit_template(p: Principal = Depends(current_principal)) -> Response:
    with system_session() as s:
        t = s.get(Tenant, p.tenant_id)
        td = {c.name: getattr(t, c.name) for c in Tenant.__table__.columns}
    with tenant_session(p.tenant_id) as s:
        d = s.scalar(select(Director).order_by(Director.shareholding_pct.desc().nullslast()))
        dep = {"full_name": d.full_name, "id_number": decrypt_pii(d.sa_id_encrypted, "sa_id")} if d else {"full_name": "", "id_number": ""}
    pdf = letters.bbbee_affidavit_template(td, dep)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="BBBEE_Sworn_Affidavit_prefilled.pdf"'})


# ── catalog ────────────────────────────────────────────────────────────────────────────────
@router.get("/catalog")
def catalog(p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        items = [{"id": str(c.id), "sku_code": c.sku_code, "item_name": c.item_name, "technical_specs": c.technical_specs,
                  "unit_of_measure": c.unit_of_measure, "unit_price_excl_vat": float(c.unit_price_excl_vat),
                  "lead_time_days": c.lead_time_days, "local_content_pct": float(c.local_content_pct) if c.local_content_pct is not None else None}
                 for c in s.scalars(select(CatalogItem).order_by(CatalogItem.sku_code))]
    return {"items": items, "embedding_provider": provider()}


@router.post("/catalog/import")
async def catalog_import(file: UploadFile = File(...), p: Principal = Depends(current_principal)) -> dict:
    """CSV columns: sku_code,item_name,technical_specs,unit_of_measure,unit_price_excl_vat,lead_time_days,local_content_pct"""
    text = (await _read(file)).decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    errors, items = [], []
    for i, r in enumerate(rows, 2):
        try:
            items.append({
                "sku_code": r["sku_code"].strip(), "item_name": r["item_name"].strip(), "technical_specs": (r.get("technical_specs") or "").strip(),
                "unit_of_measure": (r.get("unit_of_measure") or "each").strip(),
                "unit_price_excl_vat": Decimal(str(r["unit_price_excl_vat"]).replace("R", "").replace(",", "").strip()),
                "lead_time_days": int(r["lead_time_days"]) if r.get("lead_time_days") else None,
                "local_content_pct": Decimal(r["local_content_pct"]) if r.get("local_content_pct") else None,
            })
        except (KeyError, InvalidOperation, ValueError) as exc:
            errors.append(f"row {i}: {exc}")
    vectors = embed_many([f"{it['item_name']} {it['technical_specs']} {it['unit_of_measure']}" for it in items])
    with tenant_session(p.tenant_id) as s:
        for it, vec in zip(items, vectors):
            c = s.scalar(select(CatalogItem).where(CatalogItem.sku_code == it["sku_code"]))
            if c is None:
                c = CatalogItem(tenant_id=p.tenant_id, **it)
                s.add(c)
            else:
                for k, v in it.items():
                    setattr(c, k, v)
            c.embedding = vec
    return {"imported": len(items), "errors": errors, "embedding_provider": provider()}


# ── tenders ────────────────────────────────────────────────────────────────────────────────
@router.get("/portals")
def portals() -> list[dict]:
    return portal_catalogue()


@router.get("/tenders")
def list_tenders(tier: str | None = None, q: str | None = None, include_closed: bool = False, p: Principal = Depends(current_principal)) -> dict:
    from datetime import datetime, timezone

    with tenant_session(p.tenant_id) as s:
        stmt = select(Tender)
        if tier:
            stmt = stmt.where(Tender.source_tier == tier)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Tender.title.ilike(like) | Tender.tender_reference.ilike(like) | Tender.issuing_entity.ilike(like))
        if not include_closed:
            stmt = stmt.where((Tender.closing_timestamp.is_(None)) | (Tender.closing_timestamp > datetime.now(timezone.utc)))
        rows = list(s.scalars(stmt.order_by(Tender.closing_timestamp.asc().nullslast()).limit(500)))
        bid_map = {b.tender_id: b for b in s.scalars(select(BidPackage))}
        return {"tenders": [{**ser.tender(t, brief=True), "bid": {"id": str(bid_map[t.id].id), "status": bid_map[t.id].status,
                                                                      "readiness_score_pct": float(bid_map[t.id].readiness_score_pct or 0)} if t.id in bid_map else None}
                            for t in rows]}


@router.post("/tenders/sync")
def sync_portals(p: Principal = Depends(current_principal)) -> dict:
    from ..worker import harvest_all

    r = _dispatch(harvest_all)
    return {"queued": True, "task_id": getattr(r, "id", None)}


@router.post("/tenders/manual")
async def manual_tender(file: UploadFile = File(...), p: Principal = Depends(current_principal)) -> dict:
    data = await _read(file)
    try:
        tender_id = tenders.create_manual_tender(str(p.tenant_id), file.filename or "tender_pack.zip", data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with tenant_session(p.tenant_id) as s:
        t = s.get(Tender, uuid.UUID(tender_id))
        bid = bids.get_or_create_bid(s, t)
        audit_bid(s, bid)
        return {"tender": ser.tender(t), "bid": ser.bid(bid)}


@router.get("/tenders/{tender_id}")
def get_tender(tender_id: uuid.UUID, p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        t = s.get(Tender, tender_id) or _404()
        b = s.scalar(select(BidPackage).where(BidPackage.tender_id == t.id))
        return {"tender": ser.tender(t), "bid": ser.bid(b) if b else None}


@router.post("/tenders/{tender_id}/bid")
def start_bid(tender_id: uuid.UUID, p: Principal = Depends(current_principal)) -> dict:
    from ..worker import fetch_tender_pack

    with tenant_session(p.tenant_id) as s:
        t = s.get(Tender, tender_id) or _404()
        b = bids.get_or_create_bid(s, t)
        need_pack = not t.preflight.get("fields") and any(u.startswith("http") for u in t.source_files_sha256 or {})
        audit_bid(s, b)
        out = {"tender": ser.tender(t), "bid": ser.bid(b)}
    if need_pack:
        _dispatch(fetch_tender_pack, str(p.tenant_id), str(tender_id))
    return out


# ── bids / workspace ────────────────────────────────────────────────────────────────────────
def _bid(s, bid_id: uuid.UUID) -> BidPackage:  # noqa: ANN001
    return s.get(BidPackage, bid_id) or _404()


@router.get("/bids/{bid_id}")
def get_bid(bid_id: uuid.UUID, p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        return {"bid": ser.bid(b), "tender": ser.tender(s.get(Tender, b.tender_id))}


@router.post("/bids/{bid_id}/audit")
def rerun_audit(bid_id: uuid.UUID, p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        t = s.get(Tender, b.tender_id)
        pack = tenders.load_pack(s, t)
        if pack:
            tenders.apply_preflight(s, t, *pack)
        audit_bid(s, b)
        return {"bid": ser.bid(b), "tender": ser.tender(t)}


@router.get("/bids/{bid_id}/compliance")
def compliance_matrix(bid_id: uuid.UUID, p: Principal = Depends(current_principal)) -> dict:
    from ..bids.readiness import LABELS, required_categories

    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        t = s.get(Tender, b.tender_id)
        with system_session() as ss:
            tenant = ss.get(Tenant, p.tenant_id)
            req, sector = required_categories(t, tenant)
        docs = {}
        for d in s.scalars(select(VaultDocument).where(VaultDocument.is_current.is_(True), VaultDocument.doc_category.is_not(None)).order_by(VaultDocument.uploaded_at)):
            docs[d.doc_category] = d
        rows = []
        for cat in req + list(sector.values()):
            d = docs.get(cat)
            rows.append({"doc_category": cat.value, "label": LABELS[cat], "sector_plugin": cat in sector.values(),
                         "status": (d.validation_status.value if d and d.validation_status else ("PROCESSING" if d else "MISSING")),
                         "expiry_date": ser.iso(d.expiry_date) if d else None, "document_id": str(d.id) if d else None,
                         "errors": d.validation_errors if d else []})
        return {"rows": rows, "schedules": t.mandatory_schedules}


class BriefingPackIn(BaseModel):
    representative: dict
    signatory: dict


@router.post("/bids/{bid_id}/briefing/prepare")
def briefing_prepare(bid_id: uuid.UUID, body: BriefingPackIn, p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        try:
            out = bids.prepare_briefing_pack(s, b, body.representative, body.signatory, p.email)
        except bids.GateError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {**out, "bid": ser.bid(b)}


@router.post("/bids/{bid_id}/briefing/certificate")
async def briefing_cert(bid_id: uuid.UUID, file: UploadFile = File(...), p: Principal = Depends(current_principal)) -> dict:
    data = await _read(file)
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        try:
            out = bids.upload_attendance_cert(s, b, file.filename or "attendance.pdf", data, p.email)
        except (bids.GateError, UploadRejected) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {**out, "bid": ser.bid(b)}


@router.post("/bids/{bid_id}/addenda/{addendum_id}/acknowledge")
def ack_addendum(bid_id: uuid.UUID, addendum_id: str, body: dict, p: Principal = Depends(current_principal)) -> dict:
    signer = (body.get("signed_by") or "").strip()
    if not signer:
        raise HTTPException(400, "signed_by is required")
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        try:
            bids.acknowledge_addendum(s, b, addendum_id, f"{signer} <{p.email}>")
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"bid": ser.bid(b), "tender": ser.tender(s.get(Tender, b.tender_id))}


class PricingIn(BaseModel):
    margin_pct: float = 15.0
    overrides: dict = {}


@router.post("/bids/{bid_id}/pricing")
def price(bid_id: uuid.UUID, body: PricingIn, p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        summary = bids.auto_price(s, b, body.margin_pct, body.overrides)
        return {"pricing_summary": summary, "bid": ser.bid(b)}


@router.post("/bids/{bid_id}/sbd/autofill")
def autofill(bid_id: uuid.UUID, p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        bids.autofill_sbds(s, b, p.email)
        return {"bid": ser.bid(b)}


@router.post("/bids/{bid_id}/binder")
def binder(bid_id: uuid.UUID, p: Principal = Depends(current_principal)) -> dict:
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        try:
            out = bids.compile_hardcopy(s, b, p.email)
        except bids.ExportLocked as exc:
            s.commit()
            raise HTTPException(423, {"message": "Export locked — resolve blocking issues first", "blockers": exc.blockers}) from exc
        return {**out, "bid": ser.bid(b)}


class SubmitIn(BaseModel):
    username: str
    password: str


@router.post("/bids/{bid_id}/submit")
def submit_digital(bid_id: uuid.UUID, body: SubmitIn, p: Principal = Depends(current_principal)) -> dict:
    from ..submission.portal import adapter_for
    from ..worker import submit_portal

    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        t = s.get(Tender, b.tender_id)
        audit_bid(s, b)
        if blockers := has_blockers(b):
            s.commit()
            raise HTTPException(423, {"message": "Submission locked — resolve blocking issues first", "blockers": blockers})
        if t.submission_mode == SubmissionMode.MANUAL_HARDCOPY:
            raise HTTPException(409, "This tender requires a physical hardcopy submission")
        try:
            key, ad = adapter_for(t)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not ad.get("calibrated"):
            raise HTTPException(409, f"Portal adapter '{key}' is not calibrated for automated submission: {ad.get('notes', '')}")
        if not b.sbd_outputs:
            bids.autofill_sbds(s, b, p.email)
        b.status = "SUBMISSION_QUEUED"
    enc = encrypt_pii(json.dumps({"username": body.username, "password": body.password}), "portal-creds")
    if get_settings().celery_eager:
        threading.Thread(target=submit_portal.apply, args=((str(p.tenant_id), str(bid_id), enc),), daemon=True).start()
    else:
        submit_portal.delay(str(p.tenant_id), str(bid_id), enc)
    return {"queued": True}


@router.post("/bids/{bid_id}/submit/otp")
def submit_otp(bid_id: uuid.UUID, body: dict, p: Principal = Depends(current_principal)) -> dict:
    from ..submission.portal import provide_otp

    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        if b.status != "AWAITING_OTP":
            raise HTTPException(409, f"Bid is not awaiting an OTP (status {b.status})")
    provide_otp(str(bid_id), str(body.get("otp", "")))
    return {"accepted": True}


@router.get("/bids/{bid_id}/audit-log")
def audit_log(bid_id: uuid.UUID, p: Principal = Depends(current_principal)) -> list[dict]:
    with tenant_session(p.tenant_id) as s:
        b = _bid(s, bid_id)
        ids = [str(b.id), str(b.tender_id)]
        return [{"at": ser.iso(e.created_at), "actor": e.actor, "entity": e.entity, "action": e.action, "detail": e.detail}
                for e in s.scalars(select(AuditEvent).where(AuditEvent.entity_id.in_(ids)).order_by(AuditEvent.created_at.desc()).limit(200))]


# ── signed local downloads (dev storage backend) ───────────────────────────────────────────
@router.get("/files/local")
def local_file(key: str, exp: int, sig: str, filename: str | None = None) -> Response:
    if get_settings().storage_backend != "local" or not verify_local(key, exp, sig):
        raise HTTPException(403, "Invalid or expired link")
    data = get_storage().get(key)
    media = "application/pdf" if key.endswith(".pdf") else "application/zip" if key.endswith(".zip") else \
        "image/png" if key.endswith(".png") else "image/jpeg" if key.endswith(".jpg") else "application/octet-stream"
    disp = "inline" if media in ("application/pdf", "image/png", "image/jpeg") else "attachment"
    return Response(data, media_type=media, headers={"Content-Disposition": f'{disp}; filename="{filename or key.split("/")[-1]}"',
                                                     "Cache-Control": "private, max-age=60"})


def _404():  # noqa: ANN202
    raise HTTPException(404, "Not found")


@router.get("/health")
def health() -> dict:
    return {"ok": True, "date": date.today().isoformat()}
