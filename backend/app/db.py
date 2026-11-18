"""Database engine + tenant-scoped sessions.

Row-Level Security is enforced in PostgreSQL. Every transaction opened by a tenant session
executes ``set_config('app.current_tenant_id', <uuid>, true)`` (transaction-local) via an
``after_begin`` hook, so the RLS policies created in the initial migration can only ever see
rows of that tenant — even if application code forgets a WHERE clause.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
    return _engine


def _session_factory() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@event.listens_for(Session, "after_begin")
def _apply_tenant_guc(session: Session, transaction, connection) -> None:  # noqa: ANN001
    tenant_id = session.info.get("tenant_id")
    # Always set the GUC — an empty value makes every RLS policy evaluate to "no rows".
    connection.execute(
        text("SELECT set_config('app.current_tenant_id', :tid, true)"),
        {"tid": str(tenant_id) if tenant_id else ""},
    )


@contextmanager
def tenant_session(tenant_id: uuid.UUID | str) -> Iterator[Session]:
    """Session whose every transaction is pinned to ``tenant_id`` by RLS."""
    session = _session_factory()()
    session.info["tenant_id"] = str(tenant_id)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def system_session() -> Iterator[Session]:
    """Session WITHOUT a tenant. Only non-tenant tables (tenants, users) are visible."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
