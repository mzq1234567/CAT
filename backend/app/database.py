from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# Columns added to a model AFTER its table was first created. `Base.metadata.create_all` creates missing
# TABLES but never adds a COLUMN to an existing table, so querying a new column would raise "no such
# column". This idempotent top-up adds any that are missing, with a safe default. (Alembic owns real
# migrations in production; this only covers the SQLite dev DB.)
_RUNTIME_COLUMNS = {
    "assessments": [
        ("billing_detail_unavailable", "INTEGER DEFAULT 0"),
        ("data_quality", "VARCHAR DEFAULT 'complete'"),
        ("data_quality_message", "VARCHAR"),
        ("collection_diagnostics", "JSON"),
    ],
    "findings": [
        ("evidence_state", "VARCHAR DEFAULT 'quantified'"),
        ("counted_savings_monthly", "FLOAT"),   # nullable → callers fall back to estimated_savings_*
        ("counted_savings_annual", "FLOAT"),
    ],
}


def ensure_runtime_columns() -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in _RUNTIME_COLUMNS.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
