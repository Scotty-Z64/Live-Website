"""Initial schema, pgvector and Row-Level Security policies.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""
from alembic import op

from app.db import Base
from app import models

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Transaction-local GUC set by app.db for every tenant session. An unset/empty value
# yields NULL, so the policy matches no rows (fail closed).
TENANT_EXPR = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Snapshot of the v1 model metadata (tables are created in FK order).
    Base.metadata.create_all(bind=op.get_bind())

    for table in models.TENANT_OWNED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE also applies the policy to the table owner (the app role owns the tables).
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {TENANT_EXPR}) WITH CHECK (tenant_id = {TENANT_EXPR})"
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_catalog_embedding_hnsw ON catalog_items "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_tenders_closing ON tenders (tenant_id, closing_timestamp)")


def downgrade() -> None:
    for table in models.TENANT_OWNED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    Base.metadata.drop_all(bind=op.get_bind())
