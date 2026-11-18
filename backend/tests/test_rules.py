from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import ocr
from app.core.crypto import decrypt_pii, encrypt_pii, mask_id, pii_hash
from app.models import DocCategory as C
from app.models import ValidationStatus as V
from app.validation import parsing, rules, stamp
from app.validation.reconciliation import reconcile

FACTS = rules.TenantFacts(legal_name="Lwazi Electrical & Civils (Pty) Ltd")


def run(path, cat, facts=FACTS, today=None):
    data = path.read_bytes()
    r = ocr.extract(data)
    return rules.validate(cat, r.text, r.confidence, 0.85, facts, today=today,
                          stamp_fn=lambda: stamp.verify_commissioner_stamp(ocr.render_page_png(data, 0), r.text))


def test_sa_id_checksum():
    import generate_samples as g

    good = g.DIRECTORS[0][1]
    assert parsing.valid_sa_id(good)
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    assert not parsing.valid_sa_id(bad)
    assert not parsing.valid_sa_id("8513150234083")  # month 13


@pytest.mark.parametrize("s,expected", [
    ("31/03/2027", date(2027, 3, 31)), ("2027-03-31", date(2027, 3, 31)), ("31 March 2027", date(2027, 3, 31)),
    ("Valid until: 1st Feb 2027", date(2027, 2, 1)), ("March 5, 2027", date(2027, 3, 5)),
])
def test_dates(s, expected):
    assert parsing.parse_date(s) == expected


def test_money_and_pct():
    assert parsing.parse_money("R 4 500 000") == Decimal("4500000")
    assert parsing.parse_money("R12.5 million") == Decimal("12500000")
    assert parsing.parse_pct("Black ownership: 51,5%") == Decimal("51.5")


def test_regexes():
    assert rules.CIPC_REG_RE.match("2019/123456/07")
    assert not rules.CIPC_REG_RE.match("2019/12345/07")
    assert rules.CSD_RE.match("MAAA1234567")
    assert not rules.CSD_RE.match("MAAA123456")


@pytest.mark.parametrize("name,cat", [
    ("cipc.pdf", C.CIPC_COR14_3), ("csd.pdf", C.CSD_REPORT), ("sars_tcs.pdf", C.SARS_TCS_PIN),
    ("bbbee_affidavit.pdf", C.BBBEE_AFFIDAVIT), ("coida.pdf", C.COIDA_LOGS), ("cidb.pdf", C.CIDB_CERT),
    ("wireman.pdf", C.WIREMAN_LICENSE), ("bank.pdf", C.BANK_CONFIRMATION), ("director_id_1.pdf", C.DIRECTOR_ID),
])
def test_valid_samples_classify_and_validate(samples, name, cat):
    data = samples[name].read_bytes()
    assert rules.classify(ocr.extract(data).text) == cat
    o = run(samples[name], cat)
    assert o.status == V.VALID, o.flags


def test_cipc_extracts_directors(samples):
    o = run(samples["cipc.pdf"], C.CIPC_COR14_3)
    assert o.extracted["cipc_reg_number"] == "2019/123456/07"
    assert [d["shareholding_pct"] for d in o.directors] == [60.0, 40.0]


def test_cipc_deregistered():
    text = "Registration Number: 2019/123456/07\nEnterprise Status: Deregistration Process\n"
    o = rules.validate(C.CIPC_COR14_3, text, 1.0, 0.85, FACTS)
    assert "INVALID_ENTERPRISE_STATUS" in [f.code for f in o.flags]
    assert o.status == V.MISMATCH_ERROR


def test_csd_unverified_bank():
    text = "Supplier Number: MAAA1234567\nTax Compliance Status: Compliant\nBank Account Verification: Not verified\n"
    o = rules.validate(C.CSD_REPORT, text, 1.0, 0.85, FACTS)
    assert [f.code for f in o.flags] == ["CSD_UNVERIFIED"]


def test_tax_pin_expired(samples):
    o = run(samples["sars_tcs.pdf"], C.SARS_TCS_PIN, today=date.today() + timedelta(days=400))
    assert o.status == V.EXPIRED and o.flags[0].code == "TAX_PIN_EXPIRED"


def test_affidavit_without_stamp(samples):
    o = run(samples["bbbee_affidavit_unstamped.pdf"], C.BBBEE_AFFIDAVIT)
    assert o.status == V.MISSING_STAMP
    assert "MISSING_COMMISSIONER_STAMP" in [f.code for f in o.flags]


def test_affidavit_older_than_12_months(samples):
    o = run(samples["bbbee_affidavit.pdf"], C.BBBEE_AFFIDAVIT, today=date.today() + timedelta(days=400))
    assert o.status == V.EXPIRED


def test_turnover_tiers():
    base = "Black Ownership: {b}%\nB-BBEE Status Level: Level 2\nAnnual Turnover: R {t}\nCommissioner of Oaths\nSworn before me on 01 January 2026"
    qse_low = rules.validate(C.BBBEE_AFFIDAVIT, base.format(b=30, t="20 000 000"), 1, .85, FACTS, today=date(2026, 6, 1))
    assert "INVALID_TURNOVER_TIER" in [f.code for f in qse_low.flags]
    qse_ok = rules.validate(C.BBBEE_AFFIDAVIT, base.format(b=60, t="20 000 000"), 1, .85, FACTS, today=date(2026, 6, 1))
    assert "INVALID_TURNOVER_TIER" not in [f.code for f in qse_ok.flags]
    big = rules.validate(C.BBBEE_AFFIDAVIT, base.format(b=100, t="80 000 000"), 1, .85, FACTS, today=date(2026, 6, 1))
    assert "INVALID_TURNOVER_TIER" in [f.code for f in big.flags]
    overclaim = rules.validate(C.BBBEE_AFFIDAVIT, base.format(b=60, t="5 000 000").replace("Level 2", "Level 1"), 1, .85, FACTS, today=date(2026, 6, 1))
    assert "BBBEE_LEVEL_MISMATCH" in [f.code for f in overclaim.flags]


def test_low_ocr_confidence_requests_rescan(samples):
    data = samples["low_quality_scan.pdf"].read_bytes()
    r = ocr.extract(data)
    assert r.confidence < 0.85
    o = rules.validate(C.SARS_TCS_PIN, r.text, r.confidence, 0.85, FACTS)
    assert o.status == V.LOW_OCR_CONFIDENCE and o.extracted == {"ocr_confidence": r.confidence}


def test_reconciliation_detects_mismatch():
    a, b = pii_hash("8501150234083"), pii_hash("7906235123085")
    issues = reconcile({
        "CIPC_COR14_3": [{"sa_id_hash": a, "full_name": "A", "shareholding_pct": 60}, {"sa_id_hash": b, "full_name": "B", "shareholding_pct": 40}],
        "CSD_REPORT": [{"sa_id_hash": a, "full_name": "A", "shareholding_pct": None}],
    })
    assert issues and issues[0]["code"] == "DIRECTOR_MISMATCH"
    assert not reconcile({"CIPC_COR14_3": [{"sa_id_hash": a, "full_name": "A", "shareholding_pct": 100}],
                          "CSD_REPORT": [{"sa_id_hash": a, "full_name": "A", "shareholding_pct": None}]})


def test_envelope_encryption_roundtrip():
    tok = encrypt_pii("8501150234083", "sa_id")
    assert "8501150234083" not in tok and tok.startswith("v1.local.")
    assert decrypt_pii(tok, "sa_id") == "8501150234083"
    with pytest.raises(Exception):
        decrypt_pii(tok, "bank")  # wrong context (AAD) must fail
    assert pii_hash("850115 0234 083") == pii_hash("8501150234083")
    assert mask_id("8501150234083") == "850115*****83"
