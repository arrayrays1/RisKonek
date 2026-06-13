"""Week 8 — CFAU Operations Module column migration.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. The Week 8 CFAU Operations module
extends two existing (previously unused) tables rather than creating new
ones:

    equipment_reports  — Equipment Serviceability Reports (Modules A & B)
    incident_reports   — Post-Incident Reports (Module C)

This script adds the additive columns below if they are missing and
backfills sane defaults. Safe to run repeatedly — every column is guarded
by a column-existence check.

Run from the project root:

    python -m scripts.migrate_cfau_operations
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


# table -> {column_name: column DDL type}
NEW_COLUMNS = {
    "equipment_reports": {
        "title": "VARCHAR(200)",
        "report_type": "VARCHAR(30)",
        "report_status": "VARCHAR(20) DEFAULT 'draft'",
        "submitted_at": "DATETIME",
        "reviewed_by": "INTEGER",
        "reviewed_at": "DATETIME",
        "finding_applied_at": "DATETIME",
    },
    "incident_reports": {
        "operations_summary": "TEXT",
        "actions_taken": "TEXT",
        "challenges_encountered": "TEXT",
        "personnel_count": "INTEGER DEFAULT 0",
        "personnel_notes": "TEXT",
        "report_status": "VARCHAR(20) DEFAULT 'draft'",
        "created_at": "DATETIME",
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


def backfill(conn) -> None:
    # Ensure workflow status is never NULL on pre-existing rows.
    conn.execute(text(
        "UPDATE equipment_reports SET report_status = 'draft' "
        "WHERE report_status IS NULL"
    ))
    conn.execute(text(
        "UPDATE incident_reports SET report_status = 'draft' "
        "WHERE report_status IS NULL"
    ))
    conn.execute(text(
        "UPDATE incident_reports SET personnel_count = 0 "
        "WHERE personnel_count IS NULL"
    ))


def main():
    print("[1/3] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        for table in NEW_COLUMNS:
            added = add_columns(conn, table)
            if added:
                print(f"[2/3] {table}: added columns -> {', '.join(added)}")
            else:
                print(f"[2/3] {table}: all columns already present.")

        backfill(conn)
        print("[3/3] Backfilled workflow status / personnel defaults.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
