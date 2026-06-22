"""Week 8 enhancement — Serviceability repair-scheduling columns.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. This adds two additive, nullable
columns to the existing `equipment_reports` table:

    repair_scheduled_date  — optional date an admin schedules a repair for
    repair_notes           — optional free-text repair notes

These are reminder aids only — the system never changes Equipment.status
when the date arrives. Safe to run repeatedly; each column is guarded by
an existence check.

Run from the project root:

    python -m scripts.migrate_repair_scheduling
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


# column_name: column DDL type
NEW_COLUMNS = {
    "repair_scheduled_date": "DATE",
    "repair_notes": "TEXT",
}


def _existing_columns(conn, table: str) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(table)}


def add_columns(conn) -> list:
    have = _existing_columns(conn, "equipment_reports")
    added = []
    for column, ddl in NEW_COLUMNS.items():
        if column in have:
            continue
        conn.execute(text(f"ALTER TABLE equipment_reports ADD COLUMN {column} {ddl}"))
        added.append(column)
    return added


def main():
    print("[1/2] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = add_columns(conn)
        if added:
            print(f"[2/2] equipment_reports: added columns -> {', '.join(added)}")
        else:
            print("[2/2] equipment_reports: all columns already present.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
