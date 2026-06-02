"""One-time migration for the Upload Lifecycle & Auditability Sprint.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. This script:

  1. Adds the additive lifecycle columns to `uploaded_reports`
     (lifecycle_status, archived_at, archived_by, discarded_at, discarded_by)
     if they are missing.
  2. Backfills `lifecycle_status` for existing rows:
       - reports whose extraction status is 'confirmed' -> 'confirmed'
       - everything else                                -> 'draft'
  3. Creates the new `upload_history` table (via create_all).

Safe to run more than once — each step checks before acting.

Run from the project root:

    python -m scripts.migrate_upload_lifecycle
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


# Columns to add to uploaded_reports: name -> SQL type (SQLite/Postgres-compatible)
NEW_COLUMNS = {
    "lifecycle_status": "VARCHAR(20)",
    "archived_at": "DATETIME",
    "archived_by": "INTEGER",
    "discarded_at": "DATETIME",
    "discarded_by": "INTEGER",
}

TABLE = "uploaded_reports"


def _existing_columns(conn) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(TABLE)}


def add_missing_columns(conn) -> list:
    have = _existing_columns(conn)
    added = []
    for name, sql_type in NEW_COLUMNS.items():
        if name not in have:
            conn.execute(text(f'ALTER TABLE {TABLE} ADD COLUMN {name} {sql_type}'))
            added.append(name)
    return added


def backfill_lifecycle(conn) -> int:
    # Rows that were already confirmed become 'confirmed'; all others 'draft'.
    conn.execute(text(
        f"UPDATE {TABLE} SET lifecycle_status = 'confirmed' "
        f"WHERE lifecycle_status IS NULL AND status = 'confirmed'"
    ))
    res = conn.execute(text(
        f"UPDATE {TABLE} SET lifecycle_status = 'draft' "
        f"WHERE lifecycle_status IS NULL"
    ))
    # rowcount is best-effort across drivers
    return res.rowcount if res.rowcount is not None else -1


def main():
    print("[1/3] Ensuring tables exist (creates upload_history if missing)...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = add_missing_columns(conn)
        if added:
            print(f"[2/3] Added columns to {TABLE}: {', '.join(added)}")
        else:
            print(f"[2/3] No new columns needed on {TABLE}.")

        backfilled = backfill_lifecycle(conn)
        print(f"[3/3] Backfilled lifecycle_status on existing rows (drafts touched: {backfilled}).")

    print("Migration complete.")


if __name__ == "__main__":
    main()
