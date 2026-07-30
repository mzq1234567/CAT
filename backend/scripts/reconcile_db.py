"""
Idempotent dev-DB reconciler.

Brings an existing SQLite database created by `Base.metadata.create_all()` (before Alembic was
adopted) up to the current model schema by adding any missing columns/indexes. Safe to run
repeatedly — it only adds what is absent and never drops or rewrites data.

Usage:  python -m scripts.reconcile_db
Then :  alembic stamp head      # tell Alembic the DB is now at the latest revision
"""
from __future__ import annotations

from sqlalchemy import text

from app.database import engine

# column name -> SQL type/default to ADD if missing
ASSESSMENT_COLUMNS = {
    "tenant_id": "VARCHAR",
    "progress": "INTEGER DEFAULT 0",
    "status_message": "VARCHAR",
    "needs_review_count": "INTEGER DEFAULT 0",
    "snapshot_at": "DATETIME",
}

FINDING_COLUMNS = {
    "confidence": "FLOAT DEFAULT 0",
    "advisor_recommendation_id": "VARCHAR",
    "validation_status": "VARCHAR",
    "validation_variance_pct": "FLOAT",
    "actual_monthly_cost": "FLOAT",
    "debug_reason": "TEXT",
    "dismissed": "INTEGER DEFAULT 0",
    "dismissed_by": "VARCHAR",
    "dismissed_at": "DATETIME",
}


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def _add_missing(conn, table: str, columns: dict[str, str]) -> list[str]:
    existing = _existing_columns(conn, table)
    added = []
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            added.append(name)
    return added


def main() -> None:
    with engine.begin() as conn:
        added_a = _add_missing(conn, "assessments", ASSESSMENT_COLUMNS)
        added_f = _add_missing(conn, "findings", FINDING_COLUMNS)
        # Helpful index for tenant-scoped queries (created by migration 002).
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_assessments_tenant_id ON assessments (tenant_id)"
        ))

    print(f"assessments: added {added_a or 'nothing'}")
    print(f"findings:    added {added_f or 'nothing'}")
    print("Done. Now run:  alembic stamp head")


if __name__ == "__main__":
    main()
