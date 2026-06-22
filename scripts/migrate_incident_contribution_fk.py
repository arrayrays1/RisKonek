"""Week 8.1 enhancement — UploadedReport → Incident contribution FK.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. This adds one additive, nullable
column to the existing `uploaded_reports` table:

    incident_id  — the canonical Incident a single-incident upload (CFAU
                   post-incident / BDRRMO incident) contributed to.

It then backfills the new column for already-converted uploads using the
existing JSON linkage (extracted_data.linked_incident_id), so historical
contributions are discoverable via the indexed FK without re-converting.

Safe to run repeatedly:
  - the column add is guarded by an existence check,
  - the index uses IF NOT EXISTS,
  - the backfill only touches rows where incident_id IS NULL and a valid
    linked_incident_id is present.

Run from the project root:

    python -m scripts.migrate_incident_contribution_fk
"""

from sqlalchemy import inspect, text

from app.database import engine, SessionLocal
import app.models as models
from app.models import UploadedReport, Incident


TABLE = "uploaded_reports"
COLUMN = "incident_id"
INDEX = "ix_uploaded_reports_incident_id"


def _existing_columns(conn, table: str) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(table)}


def add_column(conn) -> bool:
    """Add the nullable incident_id FK column if absent. Returns True if added."""
    if COLUMN in _existing_columns(conn, TABLE):
        return False
    # Nullable, additive. SQLite accepts the inline REFERENCES clause; on
    # Postgres it creates a real FK constraint. Either way it's nullable so
    # existing rows remain valid.
    conn.execute(text(
        f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} INTEGER REFERENCES incidents(id)"
    ))
    return True


def add_index(conn) -> None:
    conn.execute(text(
        f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} ({COLUMN})"
    ))


def backfill() -> int:
    """Populate incident_id from extracted_data.linked_incident_id for rows
    that don't have it yet. Returns the number of rows updated.

    Done via the ORM so the JSON column is read portably across SQLite and
    Postgres. Only links to incidents that still exist.
    """
    updated = 0
    db = SessionLocal()
    try:
        valid_incident_ids = {i.id for i in db.query(Incident.id).all()}
        rows = (
            db.query(UploadedReport)
            .filter(UploadedReport.incident_id.is_(None))
            .all()
        )
        for r in rows:
            data = r.extracted_data or {}
            linked = data.get("linked_incident_id")
            if isinstance(linked, int) and linked in valid_incident_ids:
                r.incident_id = linked
                updated += 1
        if updated:
            db.commit()
        return updated
    finally:
        db.close()


def main():
    print("[1/3] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = add_column(conn)
        add_index(conn)
        if added:
            print(f"[2/3] {TABLE}: added column -> {COLUMN} (+ index {INDEX})")
        else:
            print(f"[2/3] {TABLE}: column {COLUMN} already present (index ensured)")

    n = backfill()
    print(f"[3/3] Backfilled incident_id on {n} existing upload(s) from JSON linkage.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
