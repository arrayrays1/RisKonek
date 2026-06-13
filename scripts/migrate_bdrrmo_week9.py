"""Week 9 — BDRRMO contact-details column migration.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. Week 9 (TR-BDR-07) extends the
existing `barangays` table with a single additive column for the
emergency-responder contact list the BDRRMO Chairperson maintains:

    barangays.emergency_contacts  — free-text responder list

This script adds the column if it is missing. Safe to run repeatedly —
the column is guarded by an existence check.

Run from the project root:

    python -m scripts.migrate_bdrrmo_week9
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


# table -> {column_name: column DDL type}
NEW_COLUMNS = {
    "barangays": {
        "emergency_contacts": "TEXT",
    },
}


def _existing_columns(conn, table: str) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(table)}


def add_columns(conn, table: str) -> list:
    have = _existing_columns(conn, table)
    added = []
    for column, ddl in NEW_COLUMNS[table].items():
        if column in have:
            continue
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        added.append(column)
    return added


def main():
    print("[1/2] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        for table in NEW_COLUMNS:
            added = add_columns(conn, table)
            if added:
                print(f"[2/2] {table}: added columns -> {', '.join(added)}")
            else:
                print(f"[2/2] {table}: all columns already present.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
