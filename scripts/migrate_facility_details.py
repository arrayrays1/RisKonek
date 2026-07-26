"""Critical Facility detail-fields migration.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. This adds three additive columns to
the existing `facilities` table so the Add/Edit Facility modal can capture
structured EO/MOA/MOU tracking and free-text notes:

    eo_moa_mou_status     — Available / Pending / Not Available / Not Applicable
    eo_moa_mou_reference  — e.g. "MOA No. 2026-014"
    notes                 — free-text, admin/BDRRMO-entered

All three are nullable with no default — existing rows (including
Week 5's imported `eo_moa_mou` free text) are left untouched; that legacy
column is preserved as-is and used as a display fallback when the new
`eo_moa_mou_reference` is empty. Safe to run repeatedly — every column is
guarded by an existence check.

Run from the project root:

    python -m scripts.migrate_facility_details
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


# column_name: column DDL type
NEW_COLUMNS = {
    "eo_moa_mou_status": "VARCHAR(20)",
    "eo_moa_mou_reference": "VARCHAR(150)",
    "notes": "TEXT",
}


def _existing_columns(conn, table: str) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(table)}


def add_columns(conn) -> list:
    have = _existing_columns(conn, "facilities")
    added = []
    for column, ddl in NEW_COLUMNS.items():
        if column in have:
            continue
        conn.execute(text(f"ALTER TABLE facilities ADD COLUMN {column} {ddl}"))
        added.append(column)
    return added


def main():
    print("[1/2] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = add_columns(conn)
        if added:
            print(f"[2/2] facilities: added columns -> {', '.join(added)}")
        else:
            print("[2/2] facilities: all columns already present.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
