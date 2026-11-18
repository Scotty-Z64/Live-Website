#!/usr/bin/env python3
"""Phase 5 — End-to-end browser verification (Playwright) against a running stack.

Verifies: tenant onboarding · statutory document validation · inline "Upload Missing Document Now"
prompts · compulsory briefing gate locking/unlocking · Hardcopy Binder ZIP generation · Mode A
digital submission pausing at OTP (against the dev mock portal).

    python scripts/e2e_verify.py --web http://localhost:3000 --shots ./e2e-shots
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_samples  # noqa: E402

STEPS: list[tuple[str, bool, str]] = []


def step(name: str):
    def deco(fn):
        def run(*a, **k):
            t = time.time()
            try:
                out = fn(*a, **k)
                STEPS.append((name, True, f"{time.time() - t:.1f}s"))
                print(f"  ✓ {name}")
                return out
            except Exception as exc:
                STEPS.append((name, False, str(exc)[:300]))
                print(f"  ✗ {name}: {exc}")
                raise
        return run
    return deco


def shot(page: Page, shots: Path, name: str):
    page.screenshot(path=str(shots / f"{name}.png"), full_page=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default="http://localhost:3000")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--shots", default="e2e-shots")
    args = ap.parse_args()
    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)
    S = generate_samples.write_all(Path(tempfile.mkdtemp(prefix="ts-e2e-")), args.api)
    email = f"e2e-{uuid.uuid4().hex[:6]}@example.com"

    with sync_playwright() as p:
        exe = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
        browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        W = args.web

        @step("Tenant onboarding")
        def onboarding():
            page.goto(f"{W}/onboarding")
            for name, val in [("legal_name", "Lwazi Electrical & Civils (Pty) Ltd"), ("full_name", "Thandiwe Mokoena"), ("email", email),
                              ("password", "correct-horse-battery"), ("turnover", "4500000"), ("black", "100"), ("black_women", "60"),
                              ("phone", "011 000 0000"), ("address", "12 Main Rd, Johannesburg")]:
                page.fill(f"input[name={name}]", val)
            page.get_by_role("button", name="electrical").click()
            page.get_by_role("button", name="Create workspace").click()
            page.wait_for_url("**/vault**")
            expect(page.get_by_test_id("tenant-name")).to_have_text("Lwazi Electrical & Civils (Pty) Ltd")

        @step("Statutory validation via Bulk Drop (CIPC, CSD, SARS → VALID)")
        def bulk():
            page.get_by_test_id("bulk-drop-input").set_input_files([str(S["cipc.pdf"]), str(S["csd.pdf"]), str(S["sars_tcs.pdf"])])
            for cat in ("CIPC_COR14_3", "CSD_REPORT", "SARS_TCS_PIN"):
                expect(page.get_by_test_id(f"slot-status-{cat}")).to_have_text("VALID", timeout=45_000)

        @step("Rejections: unstamped affidavit → MISSING STAMP, blurry scan → LOW OCR CONFIDENCE")
        def rejects():
            page.get_by_test_id("slot-input-BBBEE_AFFIDAVIT").set_input_files(str(S["bbbee_affidavit_unstamped.pdf"]))
            expect(page.get_by_test_id("slot-status-BBBEE_AFFIDAVIT")).to_have_text("MISSING STAMP", timeout=45_000)
            expect(page.get_by_test_id("slot-BBBEE_AFFIDAVIT")).to_contain_text("Commissioner of Oaths")
            page.get_by_test_id("slot-input-COIDA_LOGS").set_input_files(str(S["low_quality_scan.pdf"]))
            expect(page.get_by_test_id("slot-status-COIDA_LOGS")).to_have_text("LOW OCR CONFIDENCE", timeout=45_000)
            expect(page.get_by_test_id("slot-COIDA_LOGS")).to_contain_text("Re-scan at 300 DPI")
            shot(page, shots, "01_vault")

        @step("Catalog import")
        def catalog():
            page.goto(f"{W}/catalog")
            page.get_by_test_id("catalog-input").set_input_files(str(S["catalog.csv"]))
            expect(page.get_by_test_id("catalog-msg")).to_contain_text("5 items imported")

        @step("Manual RFP pack → pre-flight → workspace with briefing banner")
        def rfp():
            page.goto(f"{W}/tenders")
            page.get_by_test_id("manual-rfp-input").set_input_files(str(S["tender_pack.zip"]))
            page.wait_for_url("**/workspace", timeout=60_000)
            expect(page.get_by_test_id("briefing-banner")).to_be_visible()
            expect(page.get_by_test_id("gate-status")).to_have_text("PENDING REGISTRATION")
            expect(page.get_by_test_id("preflight-panel")).to_contain_text("COJ/EL/2026/045")
            expect(page.get_by_test_id("preflight-panel")).to_contain_text("80/20")
            expect(page.get_by_test_id("download-binder")).to_be_disabled()
            shot(page, shots, "02_workspace_locked")

        @step("Inline 'Upload Missing Document Now' resolves blockers in place")
        def inline():
            drawer = page.get_by_test_id("action-drawer")
            expect(drawer.get_by_role("button", name="Upload Missing Document Now").first).to_be_visible()
            for cat, files in [("CIDB_CERT", [S["cidb.pdf"]]), ("WIREMAN_LICENSE", [S["wireman.pdf"]]),
                               ("DIRECTOR_ID", [S["director_id_1.pdf"], S["director_id_2.pdf"]])]:
                page.get_by_test_id(f"fix-{cat}").first.set_input_files([str(f) for f in files])
                expect(page.get_by_test_id(f"fix-{cat}")).to_have_count(0, timeout=45_000)
            # Previously-rejected docs are re-uploaded from the drawer too
            page.get_by_test_id("fix-BBBEE_AFFIDAVIT").set_input_files(str(S["bbbee_affidavit.pdf"]))
            expect(page.get_by_test_id("fix-BBBEE_AFFIDAVIT")).to_have_count(0, timeout=45_000)
            page.get_by_test_id("fix-COIDA_LOGS").set_input_files(str(S["coida.pdf"]))
            expect(page.get_by_test_id("fix-COIDA_LOGS")).to_have_count(0, timeout=45_000)
            page.goto(f"{W}/vault")
            page.get_by_test_id("slot-input-COMPANY_PROFILE").set_input_files(str(S["company_profile.pdf"]))
            expect(page.get_by_test_id("slot-status-COMPANY_PROFILE")).to_have_text("VALID", timeout=45_000)
            page.go_back()
            page.wait_for_url("**/workspace")

        @step("Briefing gate: Prepare Briefing Pack keeps the bid LOCKED")
        def gate_lock():
            page.get_by_test_id("briefing-banner").get_by_role("button", name="Prepare Briefing Pack").click()
            page.fill("input[name=rep_name]", "Sipho Dlamini")
            page.fill("input[name=rep_id]", "7906235123085")
            page.fill("input[name=rep_position]", "Site Manager")
            page.fill("input[name=sig_name]", "Thandiwe Mokoena")
            with ctx.expect_page() as reg_tab:
                page.get_by_role("button", name="Generate Letter of Authority & open registration").click()
            expect(page.get_by_test_id("briefing-result")).to_contain_text("Letter of Authority")
            reg_tab.value.close()
            page.keyboard.press("Escape")
            expect(page.get_by_test_id("gate-status")).to_have_text("REGISTERED AWAITING CERT")
            expect(page.get_by_test_id("issue-BRIEFING_GATE_LOCKED")).to_contain_text("Upload Attendance Cert")
            expect(page.get_by_test_id("download-binder")).to_be_disabled()

        @step("Briefing gate: uploading Certificate of Attendance UNLOCKS it")
        def gate_unlock():
            page.get_by_test_id("fix-attendance").set_input_files(str(S["attendance_certificate.pdf"]))
            expect(page.get_by_test_id("toast")).to_contain_text("briefing gate unlocked")
            expect(page.get_by_test_id("briefing-banner")).to_have_count(0)
            expect(page.get_by_test_id("issue-BRIEFING_GATE_LOCKED")).to_have_count(0)

        @step("BoQ matched to catalog with live 15% VAT calculator")
        def pricing():
            page.get_by_role("tab", name="BoQ & Catalog Pricing").click()
            page.get_by_test_id("margin-input").fill("20")
            page.get_by_test_id("match-price").click()
            expect(page.get_by_test_id("pricing-grid")).to_contain_text("LED-150-IP66", timeout=30_000)
            expect(page.get_by_test_id("blocker-count")).to_have_text("Ready to submit", timeout=30_000)
            expect(page.get_by_test_id("readiness-score")).to_have_text("100%")
            shot(page, shots, "03_pricing_ready")

        @step("Auto-Fill All SBDs (AcroForm + overlay)")
        def sbds():
            page.get_by_role("button", name="Auto-Fill All SBDs").click()
            expect(page.get_by_test_id("sbd-preview")).to_contain_text("overlay", timeout=30_000)
            expect(page.get_by_test_id("sbd-preview")).to_contain_text("acroform")
            shot(page, shots, "04_sbd_preview")

        @step("Download Hardcopy Binder (ZIP) with 4 deliverables")
        def binder():
            with page.expect_download(timeout=90_000) as dl:
                page.get_by_test_id("download-binder").click()
            data = Path(dl.value.path()).read_bytes()
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
            assert names == ["01_Master_Bid_Submission.pdf", "02_Printable_Tab_Dividers.pdf", "03_Lever_Arch_Spine_Label.pdf",
                             "04_Wet_Ink_Signing_Map.pdf"], names
            (shots / "Hardcopy_Binder.zip").write_bytes(data)

        @step("Mode A digital submission pauses at OTP, then records receipt")
        def submit():
            page.get_by_test_id("submit-digital").click()
            page.fill("input[name=portal_user]", "supplier01")
            page.fill("input[name=portal_pass]", "portal-password")
            page.get_by_role("button", name="Start digital submission").click()
            page.keyboard.press("Escape")
            expect(page.get_by_test_id("bid-status")).to_have_text("AWAITING_OTP", timeout=120_000)
            page.get_by_test_id("submit-digital").click()
            page.fill("input[name=otp]", "482913")
            page.get_by_role("button", name="Confirm submission").click()
            expect(page.get_by_test_id("submission-receipt")).to_contain_text("ETR-", timeout=120_000)
            shot(page, shots, "05_submitted")

        try:
            for fn in (onboarding, bulk, rejects, catalog, rfp, inline, gate_lock, gate_unlock, pricing, sbds, binder, submit):
                fn()
        except Exception:
            shot(page, shots, "zz_failure")
        finally:
            browser.close()

    ok = all(s[1] for s in STEPS) and len(STEPS) == 12
    print(f"\n{'PASS' if ok else 'FAIL'}: {sum(s[1] for s in STEPS)}/12 steps")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
