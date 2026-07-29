"""Logistics audit accountability columns migration.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. This script adds the additive
`reason`, `deployed_to`, and `occurred_at` columns to `audit_logs` if
they are missing.

The columns are nullable with no backfill: entries logged before this
migration simply have no structured reason / deployment / occurrence
time, and the audit views render them as "—".

Safe to run more than once — the column-existence check makes it
idempotent.

Run from the project root:

    python -m scripts.migrate_audit_logistics_fields
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


TABLE = "audit_logs"
NEW_COLUMNS = {
    "reason": "TEXT",
    "deployed_to": "VARCHAR(150)",
    "occurred_at": "DATETIME",
}


def _existing_columns(conn) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(TABLE)}


def add_columns(conn) -> list:
    have = _existing_columns(conn)
    added = []
    for name, sql_type in NEW_COLUMNS.items():
        if name in have:
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {sql_type}"))
        added.append(name)
    return added


def main():
    print("[1/2] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = add_columns(conn)
        if added:
            print(f"[2/2] Added columns on {TABLE}: {', '.join(added)}")
        else:
            print(f"[2/2] All columns already present on {TABLE}.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
