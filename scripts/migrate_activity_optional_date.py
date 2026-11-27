"""Optional activity dates migration.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. Activity dates are now optional when
a BDRRMO submits a plan (they can be set later), so this drops the NOT NULL
constraint on `activities.start_date`. Safe to run repeatedly — the ALTER
only runs while the column is still NOT NULL.

Run from the project root:

    python -m scripts.migrate_activity_optional_date
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


def _start_date_nullable(conn) -> bool:
    inspector = inspect(conn)
    for c in inspector.get_columns("activities"):
        if c["name"] == "start_date":
            return bool(c["nullable"])
    raise RuntimeError("activities.start_date column not found")


def main():
    print("[1/2] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        if _start_date_nullable(conn):
            print("[2/2] activities.start_date is already nullable.")
        else:
            conn.execute(text(
                "ALTER TABLE activities ALTER COLUMN start_date DROP NOT NULL"
            ))
            print("[2/2] activities.start_date: dropped NOT NULL.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
