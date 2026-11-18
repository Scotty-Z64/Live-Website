"""End-to-end API flow: onboarding → vault validation → tender pre-flight → blockers & inline
remediation → briefing gate lock/unlock → pricing → binder ZIP → silent addendum lock/sign-off."""
import io
import uuid
import zipfile
from datetime import date, timedelta

import fitz
import pytest


@pytest.fixture(scope="module")
def auth(client):
    r = client.post("/api/auth/register", json={
        "legal_name": "Lwazi Electrical & Civils (Pty) Ltd", "email": f"owner-{uuid.uuid4().hex[:6]}@example.com",
        "password": "correct-horse-battery", "full_name": "Thandiwe Mokoena", "annual_turnover_zar": 4_500_000,
        "black_ownership_pct": 100, "black_women_ownership_pct": 60, "physical_address": "12 Main Rd, Johannesburg",
        "contact_phone": "011 000 0000", "vat_number": "4123456789", "sectors": []})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def upload(client, auth, path, cat=None):
    data = {"doc_category": cat} if cat else {}
    r = client.post("/api/vault/upload", headers=auth, files={"file": (path.name, path.read_bytes(), "application/pdf")}, data=data)
    assert r.status_code == 200, r.text
    return r.json()["document"]


def codes(bid):
    return {i["code"] for i in bid["blocking_issues"] if i["severity"] == "BLOCKER"}


def test_full_flow(client, auth, samples):
    # 1. Vault: bulk drop auto-classifies and validates
    files = [("files", (samples[n].name, samples[n].read_bytes(), "application/pdf")) for n in ("cipc.pdf", "csd.pdf", "sars_tcs.pdf")]
    r = client.post("/api/vault/bulk", headers=auth, files=files)
    assert all(x["ok"] for x in r.json()["results"])
    vault = client.get("/api/vault", headers=auth).json()["slots"]
    assert vault["CIPC_COR14_3"][0]["validation_status"] == "VALID"
    assert vault["CSD_REPORT"][0]["validation_status"] == "VALID"
    dirs = client.get("/api/directors", headers=auth).json()
    assert len(dirs) == 2 and "*****" in dirs[0]["sa_id_masked"]

    # duplicate upload is de-duplicated by SHA-256
    again = client.post("/api/vault/upload", headers=auth, files={"file": ("cipc.pdf", samples["cipc.pdf"].read_bytes(), "application/pdf")}).json()
    assert again["duplicate"] is True

    # unstamped affidavit is rejected with remediation
    doc = upload(client, auth, samples["bbbee_affidavit_unstamped.pdf"], "BBBEE_AFFIDAVIT")
    assert doc["validation_status"] == "MISSING_STAMP"
    assert doc["validation_errors"][0]["remediation"]
    tpl = client.get("/api/vault/templates/bbbee-affidavit", headers=auth)
    assert tpl.status_code == 200 and tpl.content[:5] == b"%PDF-"

    # 2. Manual RFP pack → pre-flight → bid with blockers
    r = client.post("/api/tenders/manual", headers=auth, files={"file": ("tender_pack.zip", samples["tender_pack.zip"].read_bytes(), "application/zip")})
    assert r.status_code == 200, r.text
    tender, bid = r.json()["tender"], r.json()["bid"]
    assert tender["tender_reference"] == "COJ/EL/2026/045" and tender["has_compulsory_briefing"]
    assert bid["briefing_gate_status"] == "PENDING_REGISTRATION"
    c = codes(bid)
    assert {"BRIEFING_GATE_LOCKED", "MISSING_COMMISSIONER_STAMP", "MISSING_SECTOR_LICENSE", "BOQ_UNPRICED"} <= c
    missing = [i for i in bid["blocking_issues"] if i["action"] and i["action"]["type"] == "UPLOAD_DOCUMENT"]
    assert {i["action"]["doc_category"] for i in missing} >= {"COIDA_LOGS", "CIDB_CERT", "WIREMAN_LICENSE", "DIRECTOR_ID"}
    assert all(i["action"]["label"] == "Upload Missing Document Now" for i in missing)
    bid_id = bid["id"]

    # binder export is locked while blockers exist
    r = client.post(f"/api/bids/{bid_id}/binder", headers=auth)
    assert r.status_code == 423

    # 3. Resolve document blockers inline
    for name, cat in [("bbbee_affidavit.pdf", "BBBEE_AFFIDAVIT"), ("coida.pdf", "COIDA_LOGS"), ("cidb.pdf", "CIDB_CERT"),
                      ("wireman.pdf", "WIREMAN_LICENSE"), ("director_id_1.pdf", "DIRECTOR_ID"), ("director_id_2.pdf", "DIRECTOR_ID"),
                      ("company_profile.pdf", "COMPANY_PROFILE")]:
        assert upload(client, auth, samples[name], cat)["validation_status"] == "VALID", name
    bid = client.get(f"/api/bids/{bid_id}", headers=auth).json()["bid"]
    assert codes(bid) == {"BRIEFING_GATE_LOCKED", "BOQ_UNPRICED"}, bid["blocking_issues"]

    # 4. Briefing gate: preparing the pack does NOT unlock the bid
    r = client.post(f"/api/bids/{bid_id}/briefing/prepare", headers=auth, json={
        "representative": {"full_name": "Sipho Dlamini", "id_number": "7906235123085", "position": "Site Manager"},
        "signatory": {"full_name": "Thandiwe Mokoena", "capacity": "Director"}})
    assert r.status_code == 200, r.text
    assert r.json()["registration_url"].startswith("https://") and r.json()["gate"] == "REGISTERED_AWAITING_CERT"
    loa = client.get(r.json()["letter_url"].replace("http://localhost:8000", ""))
    assert loa.status_code == 200 and b"%PDF" in loa.content[:10]
    bid = r.json()["bid"]
    assert "BRIEFING_GATE_LOCKED" in codes(bid)
    assert next(i for i in bid["blocking_issues"] if i["code"] == "BRIEFING_GATE_LOCKED")["action"]["type"] == "UPLOAD_ATTENDANCE_CERT"

    r = client.post(f"/api/bids/{bid_id}/briefing/certificate", headers=auth,
                    files={"file": ("attendance.pdf", samples["attendance_certificate.pdf"].read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["gate"] == "VERIFIED_ATTENDED" and r.json()["tender_reference_found"]
    assert "BRIEFING_GATE_LOCKED" not in codes(r.json()["bid"])

    # 5. Catalog + BoQ pricing with 15% VAT
    r = client.post("/api/catalog/import", headers=auth, files={"file": ("catalog.csv", samples["catalog.csv"].read_bytes(), "text/csv")})
    assert r.json()["imported"] == 5
    r = client.post(f"/api/bids/{bid_id}/pricing", headers=auth, json={"margin_pct": 20})
    p = r.json()["pricing_summary"]
    assert not p["unpriced_lines"], p
    assert [l["match"]["sku_code"] for l in p["lines"]] == ["LED-150-IP66", "POLE-9M-GALV", "CAB-4C-4MM-SWA", "CIV-TRENCH-08"]
    assert p["lines"][0]["unit_price"] == 2940.0  # 2450 * 1.20
    assert abs(p["vat"] - round(p["subtotal_excl_vat"] * 0.15, 2)) < 0.01
    bid = r.json()["bid"]
    assert codes(bid) == set(), bid["blocking_issues"]
    assert bid["readiness_score_pct"] == 100.0

    # 6. Auto-fill SBDs (tender-supplied flattened SBD 1 → overlay; others → AcroForm templates)
    r = client.post(f"/api/bids/{bid_id}/sbd/autofill", headers=auth)
    sbds = r.json()["bid"]["sbd_outputs"]
    assert sbds["SBD 1"]["method"] == "overlay" and "csd_number" in sbds["SBD 1"]["filled"]  # CSD no. verified from CSD report
    assert sbds["SBD 4"]["method"] == "acroform" and "director_1_id" in sbds["SBD 4"]["filled"]
    assert "tcs_pin" in sbds["SBD 1"]["filled"]

    # 7. Hardcopy binder ZIP
    r = client.post(f"/api/bids/{bid_id}/binder", headers=auth)
    assert r.status_code == 200, r.text
    zbytes = client.get(r.json()["zip_url"].replace("http://localhost:8000", "")).content
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    assert len(z.namelist()) == 4
    master = fitz.open(stream=z.read("01_Master_Bid_Submission.pdf"), filetype="pdf")
    assert master.page_count == r.json()["total_pages"]
    assert f"Page {master.page_count} of {master.page_count}" in master[-1].get_text()

    # 8. Silent addendum detected by the hash-watcher hard-locks the bid until signed off
    from app.db import tenant_session
    from app.models import Tender
    from app.tenders.harvest import watch_tenant

    tid = client.get("/api/tenant", headers=auth).json()["id"]
    with tenant_session(tid) as s:
        t = s.get(Tender, uuid.UUID(tender["id"]))
        t.source_url = "https://portal.example/tenders/45"
        t.source_files_sha256 = {**t.source_files_sha256, "https://portal.example/files/rfp.pdf": "0" * 64}
    addendum = samples["addendum_1.pdf"].read_bytes()
    pages = {"https://portal.example/tenders/45": b"<a href='/files/rfp.pdf'>RFP</a><a href='/files/addendum1.pdf'>Addendum 1</a>",
             "https://portal.example/files/rfp.pdf": samples["tender_pack.zip"].read_bytes(),
             "https://portal.example/files/addendum1.pdf": addendum}
    found = watch_tenant(tid, fetch=lambda u: pages[u])
    assert {f["filename"] for f in found} == {"rfp.pdf", "addendum1.pdf"}
    add = next(f for f in found if f["filename"] == "addendum1.pdf")
    assert "closing_timestamp" in add["diff"]["changed_fields"]
    bid = client.get(f"/api/bids/{bid_id}", headers=auth).json()["bid"]
    assert "SILENT_ADDENDUM_DETECTED" in codes(bid)
    assert client.post(f"/api/bids/{bid_id}/binder", headers=auth).status_code == 423

    for f in found:
        r = client.post(f"/api/bids/{bid_id}/addenda/{f['id']}/acknowledge", headers=auth, json={"signed_by": "Thandiwe Mokoena"})
        assert r.status_code == 200
    new_close = (date.today() + timedelta(days=28)).isoformat()
    assert r.json()["tender"]["closing_timestamp"].startswith(new_close)
    assert "SILENT_ADDENDUM_DETECTED" not in codes(r.json()["bid"])
    r = client.post(f"/api/bids/{bid_id}/binder", headers=auth)
    assert r.status_code == 200
    titles = [s["title"] for s in r.json()["sections"]]
    assert "Addenda Acknowledgement" in titles and "Compulsory Briefing Attendance" in titles


def test_tenant_cannot_read_other_bid(client, auth):
    r = client.post("/api/auth/register", json={"legal_name": "Other (Pty) Ltd", "email": f"o-{uuid.uuid4().hex[:6]}@example.com", "password": "another-password"})
    other = {"Authorization": f"Bearer {r.json()['token']}"}
    tenders = client.get("/api/tenders", headers=auth).json()["tenders"]
    bid_id = next(t["bid"]["id"] for t in tenders if t["bid"])
    assert client.get(f"/api/bids/{bid_id}", headers=other).status_code == 404
