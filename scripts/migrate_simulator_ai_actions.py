"""Saved-scenario AI suggested-actions migration.

`models.Base.metadata.create_all()` (run on app startup) creates *new* tables
but never ALTERs existing ones. This adds one additive column to the existing
`saved_scenarios` table:

    ai_actions_json  — JSON blob holding the validated `suggested_actions`
                       list and the closing advisory note produced with the
                       saved AI briefing.

Nullable with no default: rows saved before this column existed keep their
briefing and simply render no suggested actions. Safe to run repeatedly — the
column is guarded by an existence check.

Run from the project root:

    python -m scripts.migrate_simulator_ai_actions
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


NEW_COLUMNS = {
    "ai_actions_json": "TEXT",
}


def _existing_columns(conn, table: str) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(table)}


def add_columns(conn) -> list:
    have = _existing_columns(conn, "saved_scenarios")
    added = []
    for column, ddl in NEW_COLUMNS.items():
        if column in have:
            continue
        conn.execute(text(f"ALTER TABLE saved_scenarios ADD COLUMN {column} {ddl}"))
        added.append(column)
    return added


def main():
    print("[1/2] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = add_columns(conn)
        if added:
            print(f"[2/2] saved_scenarios: added columns -> {', '.join(added)}")
        else:
            print("[2/2] saved_scenarios: all columns already present.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
