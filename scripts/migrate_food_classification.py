"""Food classification — add Resource.food_type column.

`models.Base.metadata.create_all()` (run on app startup) creates *new*
tables but never ALTERs existing ones. This adds one additive, nullable
column to the existing `resources` table:

    food_type  — optional food sub-classification (rice, canned_goods,
                 drinking_water, rte_meals, baby_food, medical_nutrition),
                 only meaningful when category == 'food'; NULL otherwise.

No backfill is needed — existing rows keep food_type = NULL (unclassified),
which the UI renders as "—". Safe to run repeatedly; the column add is
guarded by an existence check. A fresh DB gets the column from create_all()
and does not need this script.

Run from the project root:

    python -m scripts.migrate_food_classification
"""

from sqlalchemy import inspect, text

from app.database import engine
import app.models as models


def _existing_columns(conn, table: str) -> set:
    inspector = inspect(conn)
    return {c["name"] for c in inspector.get_columns(table)}


def add_column(conn) -> bool:
    if "food_type" in _existing_columns(conn, "resources"):
        return False
    conn.execute(text("ALTER TABLE resources ADD COLUMN food_type VARCHAR(30)"))
    return True


def main():
    print("[1/2] Ensuring tables exist...")
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        if add_column(conn):
            print("[2/2] resources: added column -> food_type")
        else:
            print("[2/2] resources: food_type already present.")

    print("Migration complete.")


if __name__ == "__main__":
    main()
