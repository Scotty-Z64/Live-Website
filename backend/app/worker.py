"""Celery application, task definitions and beat schedule.

  celery -A app.worker worker -l info -Q default,ocr,harvest,render,submit
  celery -A app.worker beat -l info
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from .config import get_settings

settings = get_settings()
celery_app = Celery("tendersync", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_always_eager=settings.celery_eager,
    task_eager_propagates=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_routes={
        "ts.process_vault_document": {"queue": "ocr"},
        "ts.harvest_all": {"queue": "harvest"},
        "ts.hashwatch_all": {"queue": "harvest"},
        "ts.fetch_tender_pack": {"queue": "harvest"},
        "ts.compile_binder": {"queue": "render"},
        "ts.submit_portal": {"queue": "submit"},
    },
    beat_schedule={
        "harvest-every-4h": {"task": "ts.harvest_all", "schedule": crontab(minute=7, hour="*/4")},
        "hashwatch-every-6h": {"task": "ts.hashwatch_all", "schedule": crontab(minute=23, hour="*/6")},
    },
    timezone="Africa/Johannesburg",
)


@celery_app.task(name="ts.process_vault_document", autoretry_for=(ConnectionError,), retry_backoff=True, max_retries=3)
def process_vault_document(tenant_id: str, doc_id: str) -> dict:
    from .validation.pipeline import process_document

    return process_document(tenant_id, doc_id)


@celery_app.task(name="ts.harvest_all")
def harvest_all(only: list[str] | None = None) -> dict:
    from .tenders.harvest import harvest_all as run

    return run(only)


@celery_app.task(name="ts.hashwatch_all")
def hashwatch_all() -> dict:
    from .tenders.harvest import watch_all

    return watch_all()


@celery_app.task(name="ts.fetch_tender_pack")
def fetch_tender_pack(tenant_id: str, tender_id: str) -> dict:
    from .tenders.harvest import fetch_tender_pack as run

    return run(tenant_id, tender_id)


@celery_app.task(name="ts.submit_portal", time_limit=60 * 30)
def submit_portal(tenant_id: str, bid_id: str, encrypted_credentials: str) -> dict:
    from .core.events import bid_channel, publish
    from .db import tenant_session
    from .models import AuditEvent, BidPackage
    from .submission.portal import submit

    try:
        return submit(tenant_id, bid_id, encrypted_credentials)
    except Exception as exc:
        import uuid

        with tenant_session(tenant_id) as s:
            bid = s.get(BidPackage, uuid.UUID(bid_id))
            bid.status = "SUBMISSION_FAILED"
            bid.submission_receipt = {**(bid.submission_receipt or {}), "digital_error": str(exc)[:500]}
            s.add(AuditEvent(tenant_id=bid.tenant_id, entity="bid", entity_id=bid_id, action="SUBMISSION_FAILED", detail={"error": str(exc)[:500]}))
        publish(bid_channel(tenant_id, bid_id), "submission.failed", {"error": str(exc)[:300]})
        raise
