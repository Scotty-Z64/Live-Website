# TenderSync SA — Automated South African Bid & Compliance Engine

Multi-tenant B2B SaaS that harvests South African tenders, runs a **pre-flight RFP analysis** before
any client data is touched, validates statutory documents (CIPC, CSD, SARS, B-BBEE, COIDA, CIDB,
PSIRA, Wireman's Licence), enforces the **compulsory-briefing gate**, auto-fills SBD forms, and
outputs either a **digital portal submission** (Mode A) or a **print-ready hardcopy binder** (Mode B).

Built from the *TenderSync SA — Master Architecture & Claude Code Execution Blueprint*.

```
backend/   FastAPI · SQLAlchemy 2 · Alembic · Celery + Redis · LangGraph · PyMuPDF/WeasyPrint/ReportLab
frontend/  Next.js 15 (App Router) · TypeScript · Tailwind v4 · shadcn-style Radix components
scripts/   preflight_check.py · generate_samples.py · e2e_verify.py
docker/    PostgreSQL init (non-superuser app role so RLS is enforced)
```

## Quick start

### Docker Compose (full stack)
```bash
cp .env.example .env                       # fill in keys; dev runs without cloud keys
docker compose up --build                  # postgres+pgvector, redis, clamav, migrate, api, worker, beat, web
open http://localhost:3000/onboarding
```
The `api` container runs `scripts/preflight_check.py` before starting uvicorn.

### Bare metal (what the verification in this repo used)
```bash
# PostgreSQL 16 + pgvector, Redis 7 running locally; then as postgres superuser:
psql -f docker/postgres/init.sql
python -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements.txt
cp .env.example .env
python scripts/preflight_check.py
cd backend && alembic upgrade head
uvicorn app.main:app --port 8000 &
celery -A app.worker worker -Q default,ocr,harvest,render,submit -l info &
celery -A app.worker beat -l info &
cd ../frontend && npm ci && npm run build && npm start
```

## Verification

| Suite | Command | Result at time of writing |
|---|---|---|
| Backend unit + integration (39 tests: validators, stamp detection, pre-flight, SBD AcroForm/overlay, three-pass binder, ClamAV INSTREAM, OCDS/HTML harvest parsing, **RLS isolation**, full API flow incl. hash-watcher) | `cd backend && pytest` | 39 passed |
| Phase 5 browser E2E (Playwright, live stack with real Celery worker) | `python scripts/e2e_verify.py` | 12/12 steps |
| Frontend | `cd frontend && npx tsc --noEmit && npm run build` | clean |

The E2E covers: tenant onboarding · Bulk Drop classification & validation · unstamped affidavit →
`MISSING_STAMP` · blurry scan → `LOW_OCR_CONFIDENCE` · manual RFP pack → pre-flight · inline
**Upload Missing Document Now** · **Prepare Briefing Pack** keeps bid locked in
`REGISTERED_AWAITING_CERT` · attendance-cert upload unlocks (`VERIFIED_ATTENDED`) · catalog match +
15% VAT · Auto-Fill All SBDs · **Hardcopy Binder ZIP** (4 files) · Mode A submission pausing at OTP
and recording the portal reference.

`scripts/generate_samples.py` produces watermarked **SAMPLE** fixtures (fictitious company,
directors with checksum-valid IDs, tender pack with nested ZIP, BoQ spreadsheet, addendum).

## Blueprint → code map

| Blueprint | Implementation |
|---|---|
| §2 POPIA topology, PII tokenisation | `app/core/crypto.py` (per-value AES-256-GCM data keys wrapped by AWS KMS; HMAC lookup hash), `app/core/storage.py` (S3 af-south-1 SSE-KMS, signed URLs) |
| §3 env + pre-flight | `.env.example`, `scripts/preflight_check.py` (PG16/pgvector/RLS-forced/non-superuser, Redis 7 pub/sub, S3 region+versioning+KMS+public-block, KMS round-trip, Azure DI models, Anthropic model, 1536-dim embeddings, ClamAV EICAR, Chromium) |
| §4 schema + RLS | `app/models.py`, `alembic/versions/0001_initial_schema_rls.py` — `FORCE ROW LEVEL SECURITY` + `tenant_isolation` policy on every tenant table bound to `app.current_tenant_id`, set per transaction in `app/db.py` |
| §5 harvesting (4h) + hash-watcher (6h) | `app/tenders/harvesters/*` (eTenders OCDS API, HTTP + Playwright listing adapters, 27-portal registry), `app/tenders/harvest.py`, Celery beat in `app/worker.py` |
| §6 pre-flight + briefing gate | `app/tenders/preflight.py` + `graph.py` (LangGraph: unpack → segment → rules → enrich → assemble, thread id `tenant_id:tender_id`, every field cited), `app/bids/service.py` gate transitions |
| §7 validation + SBD filling | `app/validation/{rules,stamp,reconciliation,pipeline}.py`, `app/ocr`, `app/pdf/sbd.py` |
| §8 Mode A / Mode B | `app/submission/portal.py`, `app/pdf/binder.py` |
| §9 UI | `frontend/app/{vault,tenders,tenders/[id]/workspace,catalog}` — 65/35 split, WebSocket Action Drawer (`/ws/bids/{id}`) |

## Key behaviours
* **Zero ungrounded legal text.** SBD slots are filled only from verified vault/profile values;
  YES/NO declarations and signatures are never answered — they go to `04_Wet_Ink_Signing_Map.pdf`.
  The optional Claude enrichment step may only fill pre-flight fields the deterministic rules missed,
  and its answer is discarded unless its quote appears verbatim in the tender pack.
* **OCR gate.** Born-digital PDFs are read from the text layer (confidence 1.0). Scans go to Azure
  Document Intelligence; mean word confidence < 0.85 → `LOW_OCR_CONFIDENCE`, no values guessed.
* **Stamp verification.** OpenCV finds ink-stamp regions (coloured masks + Hough circles with ring
  coverage). With an Anthropic key, Claude must also confirm a Commissioner of Oaths stamp + signature
  whose bounding box overlaps an OpenCV region.
* **Export locks.** Binder download and digital submission return HTTP 423 while any blocker exists
  (missing/expired documents, TCS PIN not valid to closing + 30 days, CIDB grade, director mismatch,
  briefing gate, unacknowledged addendum, unpriced BoQ).
* **Briefing gate.** The platform never registers on the bidder's behalf: it generates the Letter of
  Authority and opens the RSVP link; only an uploaded Certificate of Attendance unlocks the bid.

## What needs your input before production
These parts are built and tested against fixtures, but depend on things this build environment could
not reach:

1. **Portal calibration.** The sandbox's network blocked all SA procurement sites, so the 27 portal
   URLs in `registry.py` are best-known addresses with `verified=False`, parsed by a generic heuristic
   table parser. Run `python -m app.cli harvest <portal>` from a network that can reach them and adjust
   URLs/adapters (overridable via `PORTAL_REGISTRY_FILE`). The eTenders OCDS adapter follows the
   published OCDS release format.
2. **Mode A selector maps.** Only the dev mock portal (`/mock-portal`, disabled in production) is
   calibrated. Each real portal needs its login/upload/OTP selectors captured with a supplier account
   (`SUBMISSION_ADAPTERS_FILE`); uncalibrated adapters refuse to run.
3. **Cloud credentials.** AWS (S3/KMS af-south-1), Azure Document Intelligence, Anthropic and OpenAI
   keys were not available here; their code paths run only when configured and were not exercised
   live. ClamAV runs in Docker Compose; without it, development accepts uploads tagged `SKIPPED_DEV`
   (production rejects them).
4. **Official SBD forms.** Bundled SBD layouts are reproductions for completion; when a tender pack
   includes the entity's own SBDs those are filled instead (AcroForm first, overlay fallback).
5. **PDF/A-1b** conversion uses Ghostscript (installed in the Docker image); without it Mode A sends
   compressed standard PDFs and records `pdfa: false` in the receipt.
6. Docker Compose was written but not run here (no Docker daemon in the build sandbox).
