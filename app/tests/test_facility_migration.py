"""Tests for scripts/migrate_facility_details.py — the guarded migration
that adds eo_moa_mou_status / eo_moa_mou_reference / notes to `facilities`.

Runs against a temporary on-disk SQLite file (not the real instance DB) so
this never touches developer data, and exercises the same
inspect-then-ALTER pattern the script uses in production.
"""
import os
import tempfile

from sqlalchemy import create_engine, inspect, text

import app.models as models
from scripts.migrate_facility_details import NEW_COLUMNS, add_columns


def _facility_columns(conn):
    return {c["name"] for c in inspect(conn).get_columns("facilities")}


def test_migration_adds_missing_columns_and_is_idempotent():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        models.Base.metadata.create_all(bind=engine)

        with engine.begin() as conn:
            # Simulate a pre-existing DB from before these columns existed.
            for column in NEW_COLUMNS:
                conn.execute(text(f"ALTER TABLE facilities DROP COLUMN {column}"))
            before = _facility_columns(conn)
            assert not (before & set(NEW_COLUMNS))

            added_first = add_columns(conn)
            assert set(added_first) == set(NEW_COLUMNS)
            after_first = _facility_columns(conn)
            assert set(NEW_COLUMNS) <= after_first

            # Re-running must be a no-op, not an error (guarded by an
            # existence check per column).
            added_second = add_columns(conn)
            assert added_second == []
    finally:
        engine.dispose()
        os.remove(path)


def test_migration_preserves_existing_rows():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        models.Base.metadata.create_all(bind=engine)

        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO barangays (id, name) VALUES (1, 'Test Barangay')"
            ))
            conn.execute(text(
                "INSERT INTO facilities "
                "(id, barangay_id, name, facility_type, latitude, longitude, "
                " operational_status, capacity_families, vulnerability_risk) "
                "VALUES (1, 1, 'Imported Hall', 'evacuation_center', 14.35, 121.05, "
                "'available', '40-80', 'Flooding — Moderate Risk')"
            ))
            for column in NEW_COLUMNS:
                try:
                    conn.execute(text(f"ALTER TABLE facilities DROP COLUMN {column}"))
                except Exception:
                    pass

            add_columns(conn)

            row = conn.execute(text(
                "SELECT name, capacity_families, vulnerability_risk, "
                "eo_moa_mou_status, notes FROM facilities WHERE id = 1"
            )).fetchone()
            assert row.name == "Imported Hall"
            assert row.capacity_families == "40-80"          # legacy range preserved
            assert row.vulnerability_risk == "Flooding — Moderate Risk"
            assert row.eo_moa_mou_status is None              # new column, no default
            assert row.notes is None
    finally:
        engine.dispose()
        os.remove(path)
