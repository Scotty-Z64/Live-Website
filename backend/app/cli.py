"""Operational commands.

    python -m app.cli reindex-catalog      # re-embed every catalog item (after switching embedding provider)
    python -m app.cli harvest [portal ...] # run the harvesters now (synchronously)
    python -m app.cli hashwatch            # run the silent-addenda hash-watcher now
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import select


def reindex_catalog() -> None:
    from .bids.embeddings import embed_many, provider
    from .db import system_session, tenant_session
    from .models import CatalogItem, Tenant

    with system_session() as s:
        tenant_ids = list(s.scalars(select(Tenant.id)))
    total = 0
    for tid in tenant_ids:
        with tenant_session(tid) as s:
            items = list(s.scalars(select(CatalogItem)))
            vecs = embed_many([f"{i.item_name} {i.technical_specs or ''} {i.unit_of_measure or ''}" for i in items])
            for i, v in zip(items, vecs):
                i.embedding = v
            total += len(items)
    print(f"re-embedded {total} catalog items with {provider()}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *rest = argv
    if cmd == "reindex-catalog":
        reindex_catalog()
    elif cmd == "harvest":
        from .tenders.harvest import harvest_all

        print(json.dumps(harvest_all(rest or None), indent=2, default=str))
    elif cmd == "hashwatch":
        from .tenders.harvest import watch_all

        print(json.dumps(watch_all(), indent=2))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
