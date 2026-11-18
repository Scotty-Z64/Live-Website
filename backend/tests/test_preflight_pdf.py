import io
import socket
import struct
import threading
import zipfile

import fitz
import pytest

from app.core import clamav
from app.pdf import binder, sbd
from app.tenders import preflight
from app.tenders.graph import run_preflight
from app.tenders.harvesters.adapters import OcdsHarvester
from app.tenders.harvesters.base import parse_listing_html
from app.tenders.harvesters.registry import load_portals


def test_preflight_full_pack(samples):
    st = run_preflight("tenant-a", "tender-1", "pack.zip", samples["tender_pack.zip"].read_bytes())
    f = st["fields"]
    assert f["tender_reference"] == "COJ/EL/2026/045"
    assert f["preference_system"] == "80_20"
    assert f["functionality_threshold_pct"] == 70.0
    assert f["cidb_min_grade"] == {"grade": 5, "class": "EP"}
    assert f["submission_mode"] == "HYBRID"
    assert f["initial_every_page"] is True
    b = f["briefing"]
    assert b["compulsory"] and b["timestamp"] and b["registration_url"].startswith("https://")
    assert "Jorissen" in b["venue_or_link"]
    assert {"SBD 1", "SBD 3.1", "SBD 4", "SBD 6.1", "SBD 6.2"} <= {s["name"] for s in f["mandatory_schedules"]}
    assert {p["plugin"] for p in f["sector_plugins"]} >= {"CIDB", "WIREMAN", "SANS"}
    assert all(st["zones"][z]["spans"] for z in ("T1", "T2", "C1C2", "C3"))
    assert len(st["boq"]) == 4 and st["boq"][0]["quantity"] == 120
    # every populated scalar field carries a provenance citation
    for k in ("tender_reference", "closing_timestamp", "preference_system", "functionality_threshold_pct"):
        assert st["citations"][k]["snippet"]


def test_nested_zip_and_zip_slip_guard():
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("../../evil.pdf", b"%PDF-1.4 x")
        z.writestr("ok.txt", b"Tender Number: ABC/1")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("inner.zip", inner.getvalue())
    files = preflight.unpack("outer.zip", outer.getvalue())
    assert [f.name for f in files] == ["inner.zip/ok.txt"]


def test_sbd_acroform_and_overlay():
    values = {"supplier_name": "Lwazi Electrical", "bid_number": "COJ/EL/2026/045", "csd_number": "MAAA1234567", "bbbee_level": "Level 1"}
    r = sbd.fill(sbd.generate_template("SBD 1"), values)
    assert r.method == "acroform" and set(values) <= set(r.filled)
    doc = fitz.open(stream=r.pdf, filetype="pdf")
    vals = {w.field_name.split(".")[-1]: w.field_value for w in doc[0].widgets()}
    assert vals["supplier_name"] == "Lwazi Electrical"
    r2 = sbd.fill(sbd.generate_template("SBD 1", with_fields=False), values)
    assert r2.method == "overlay" and set(values) <= set(r2.filled)
    text = fitz.open(stream=r2.pdf, filetype="pdf")[0].get_text()
    assert "MAAA1234567" in text and "Level 1" in text
    # unverified slots are never filled
    assert "tcs_pin" in r2.missing_values


def test_binder_three_pass(samples):
    land = fitz.open()
    land.new_page(width=842, height=595).insert_text((50, 50), "LANDSCAPE CERT")
    secs = [binder.Section("SBD Forms", [binder.Item("SBD 1", sbd.generate_template("SBD 1"), True),
                                         binder.Item("SBD 4", sbd.generate_template("SBD 4"), True)]),
            binder.Section("Certificates", [binder.Item("CIPC", samples["cipc.pdf"].read_bytes()),
                                            binder.Item("Landscape", land.tobytes()),
                                            binder.Item("Profile", samples["company_profile.pdf"].read_bytes())])]
    meta = binder.BinderMeta("Lwazi Electrical", "COJ/EL/2026/045", "LED lights", "CoJ", "2026-12-01", "TS-COJEL", initial_every_page=True)
    out = binder.compile_binder(meta, secs)
    z = zipfile.ZipFile(io.BytesIO(out["zip"]))
    assert z.namelist() == ["01_Master_Bid_Submission.pdf", "02_Printable_Tab_Dividers.pdf",
                            "03_Lever_Arch_Spine_Label.pdf", "04_Wet_Ink_Signing_Map.pdf"]
    master = fitz.open(stream=z.read("01_Master_Bid_Submission.pdf"), filetype="pdf")
    total = master.page_count
    assert total == out["total_pages"]
    for i, p in enumerate(master):
        assert abs(p.rect.width - 595.28) < 1 and abs(p.rect.height - 841.89) < 1
        t = p.get_text()
        assert f"Page {i + 1} of {total}" in t and f"TS-COJEL-{i + 1:06d}" in t
    # index links resolve to the computed section start pages
    targets = sorted({l["page"] + 1 for l in master[1].get_links()})
    assert out["sections"][0]["start_page"] in targets and out["sections"][1]["start_page"] in targets
    assert fitz.open(stream=z.read("02_Printable_Tab_Dividers.pdf"), filetype="pdf").page_count == 2
    assert out["signing_rows"], "SBD signature lines must appear on the signing map"
    assert "INITIALS ON EVERY PAGE" in fitz.open(stream=z.read("04_Wet_Ink_Signing_Map.pdf"), filetype="pdf")[0].get_text()


def test_spine_spec():
    assert binder.spine_spec(100, 5)["spine_width_mm"] == 50
    assert binder.spine_spec(450, 6)["spine_width_mm"] == 75
    big = binder.spine_spec(1500, 8)
    assert big["volumes"] == 3 and big["spine_width_mm"] == 80


def _fake_clamd(reply: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        buf = b""
        while not buf.endswith(struct.pack("!L", 0)):
            buf += conn.recv(65536)
        conn.sendall(reply + b"\0")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def test_clamav_instream(monkeypatch):
    from app.config import get_settings

    port = _fake_clamd(b"stream: OK")
    monkeypatch.setattr(get_settings(), "clamav_port", port)
    assert clamav.scan_bytes(b"hello").status == "CLEAN"
    port = _fake_clamd(b"stream: Eicar-Test-Signature FOUND")
    monkeypatch.setattr(get_settings(), "clamav_port", port)
    with pytest.raises(clamav.VirusDetected) as e:
        clamav.scan_bytes(b"X5O!P%@AP")
    assert "Eicar" in e.value.signature


def test_ocds_release_parsing():
    cfg = next(p for p in load_portals() if p.key == "etenders")
    rel = {"ocid": "ocds-9t57fa-123", "buyer": {"name": "Dept of Public Works"},
           "tender": {"id": "DPW/01/2026", "title": "Electrical maintenance", "status": "active", "mainProcurementCategory": "services",
                      "tenderPeriod": {"endDate": "2026-12-15T11:00:00+02:00"}, "documents": [{"url": "https://x/doc.pdf"}],
                      "briefingSession": {"isSession": True, "compulsory": True, "date": "2026-12-01T10:00:00", "venue": "Pretoria"}}}
    r = OcdsHarvester(cfg).parse_release(rel)
    assert r.tender_reference == "DPW/01/2026" and r.closing.year == 2026 and r.briefing["compulsory"]
    assert r.document_urls == ["https://x/doc.pdf"] and r.issuing_entity == "Dept of Public Works"


def test_listing_parser():
    cfg = next(p for p in load_portals() if p.key == "ceta")
    html = """<table><tr><th>Ref</th><th>Description</th><th>Closing</th></tr>
      <tr><td>CETA/RFP/12/2026</td><td>Appointment of service provider for learnership training</td><td>Closing: 15/12/2026 11:00</td>
          <td><a href='/docs/rfp12.pdf'>RFP</a></td></tr>
      <tr><td>Notice</td><td>Office closed</td></tr></table>"""
    recs = parse_listing_html(html, cfg, "https://www.ceta.org.za/tenders/")
    assert len(recs) == 1
    assert recs[0].tender_reference == "CETA/RFP/12/2026" and recs[0].closing.day == 15
    assert recs[0].document_urls == ["https://www.ceta.org.za/docs/rfp12.pdf"]
    assert len(load_portals()) >= 25
