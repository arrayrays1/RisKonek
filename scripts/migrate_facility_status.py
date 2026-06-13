"""Week 9 — Critical Facility status & archive column migration.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. This adds two additive columns to
the existing `facilities` table:

    operational_status  — available / under_maintenance / unavailable
    is_archived         — soft delete / archive flag

and backfills sane defaults from the existing `is_active` flag
(is_active -> available, otherwise under_maintenance). Safe to run
repeatedly — every column is guarded by an existence check.

Run from the project root:

    python -m scripts.migrate_facility_status
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


# column_name: column DDL type
NEW_COLUMNS = {
    "operational_status": "VARCHAR(20) DEFAULT 'available'",
    "is_archived": "BOOLEAN DEFAULT 0",
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


def backfill(conn) -> None:
    # Derive the operational tag from the legacy is_active flag.
    conn.execute(text(
        "UPDATE facilities SET operational_status = 'available' "
        "WHERE operational_status IS NULL AND is_active = 1"
    ))
    conn.execute(text(
        "UPDATE facilities SET operational_status = 'under_maintenance' "
        "WHERE operational_status IS NULL AND (is_active = 0 OR is_active IS NULL)"
    ))
    conn.execute(text(
        "UPDATE facilities SET is_archived = 0 WHERE is_archived IS NULL"
    ))


def main():
    print("[1/3] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = add_columns(conn)
        if added:
            print(f"[2/3] facilities: added columns -> {', '.join(added)}")
        else:
            print("[2/3] facilities: all columns already present.")
        backfill(conn)
        print("[3/3] Backfilled operational_status / is_archived defaults.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
