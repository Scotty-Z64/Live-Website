import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError, DBAPIError

from app.db import system_session, tenant_session
from app.models import Tenant, Tender


def test_role_is_not_superuser(db):
    with db.connect() as c:
        row = c.execute(text("select rolsuper, rolbypassrls from pg_roles where rolname = current_user")).one()
    assert row == (False, False), "app must connect as a non-superuser without BYPASSRLS or RLS is ineffective"


def test_tenant_isolation(db):
    with system_session() as s:
        a, b = Tenant(legal_name="A (Pty) Ltd"), Tenant(legal_name="B (Pty) Ltd")
        s.add_all([a, b])
        s.flush()
        a_id, b_id = a.id, b.id
    with tenant_session(a_id) as s:
        s.add(Tender(tenant_id=a_id, source_portal="MANUAL", tender_reference="A-1"))
    with tenant_session(b_id) as s:
        s.add(Tender(tenant_id=b_id, source_portal="MANUAL", tender_reference="B-1"))
    with tenant_session(a_id) as s:
        refs = [t.tender_reference for t in s.scalars(select(Tender))]
    assert "A-1" in refs and "B-1" not in refs
    # No tenant context → fail closed
    with system_session() as s:
        assert s.scalars(select(Tender)).all() == []
    # WITH CHECK: tenant A cannot write a row for tenant B
    with pytest.raises((ProgrammingError, DBAPIError)):
        with tenant_session(a_id) as s:
            s.add(Tender(tenant_id=b_id, source_portal="MANUAL", tender_reference="SNEAKY"))
            s.flush()
