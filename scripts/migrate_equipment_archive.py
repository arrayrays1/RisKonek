"""Week 7 — Equipment archive column migration.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. This script adds the additive
`is_archived` column to `equipment` if it is missing, and backfills
existing rows with FALSE so they show as active.

Safe to run more than once — the column-existence check makes it
idempotent.

Run from the project root:

    python -m scripts.migrate_equipment_archive
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


TABLE = "equipment"
NEW_COLUMN = "is_archived"
COLUMN_SQL = "BOOLEAN DEFAULT 0"


def _existing_columns(conn) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(TABLE)}


def add_is_archived(conn) -> bool:
    have = _existing_columns(conn)
    if NEW_COLUMN in have:
        return False
    conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {NEW_COLUMN} {COLUMN_SQL}"))
    return True


def backfill(conn) -> int:
    res = conn.execute(text(
        f"UPDATE {TABLE} SET {NEW_COLUMN} = 0 WHERE {NEW_COLUMN} IS NULL"
    ))
    return res.rowcount if res.rowcount is not None else -1


def main():
    print("[1/3] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = add_is_archived(conn)
        if added:
            print(f"[2/3] Added column {TABLE}.{NEW_COLUMN}.")
        else:
            print(f"[2/3] Column {TABLE}.{NEW_COLUMN} already present.")

        touched = backfill(conn)
        print(f"[3/3] Backfilled {NEW_COLUMN}=FALSE on rows: {touched}")

    print("Migration complete.")


if __name__ == "__main__":
    main()
