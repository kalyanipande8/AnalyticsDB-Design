"""
tests/test_pipeline.py

Tests the real behavior of the warehouse, not just that functions run:
  * Schema constraints are enforced (a bad insert -- violating a CHECK
    constraint and a FOREIGN KEY -- is rejected by SQLite, not just by
    application code).
  * The SCD2 upsert correctly preserves patient history (old version
    expired, new version current, both still queryable).
  * The validation suite correctly catches an injected data-quality
    issue (we intentionally corrupt one row and confirm the relevant
    check flips from PASS to FAIL, then confirm it reports back to PASS
    once the corruption is removed).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from build_database import build, upsert_patient_scd2, start_batch, finish_batch, make_patient_attrs
from validation_queries import run_all_checks

TEST_DB = os.path.join(os.path.dirname(__file__), "_test_analytics.db")


@pytest.fixture(scope="module")
def built_db():
    build(db_path=TEST_DB, seed=42)
    yield TEST_DB
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


@pytest.fixture
def conn(built_db):
    c = sqlite3.connect(built_db)
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_build_produces_expected_row_counts(conn):
    tables = ["dim_patient", "dim_provider", "dim_facility", "dim_diagnosis", "dim_procedure",
              "dim_medication", "dim_lab_test", "dim_date", "fact_encounter", "fact_diagnosis",
              "fact_procedure", "fact_lab_result", "fact_medication_order"]
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n > 0, f"table {t} should not be empty"
    n_encounters = conn.execute("SELECT COUNT(*) FROM fact_encounter").fetchone()[0]
    assert n_encounters == 1500


def test_foreign_key_violation_is_rejected(conn):
    """A fact row referencing a non-existent patient_key must be rejected
    by the database's own FOREIGN KEY enforcement -- this proves the
    schema constraints are real, not merely assumed."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO fact_encounter
                (encounter_id, patient_key, provider_key, facility_key, admit_date_key,
                 discharge_date_key, encounter_type, length_of_stay_days, total_charge_usd,
                 load_batch_id, loaded_at)
            VALUES ('BAD_ENC_1', 999999, 1, 1, 20240101, 20240101, 'Outpatient', 0, 100.0, 1, '2024-01-01')
        """)
        conn.commit()


def test_check_constraint_rejects_bad_encounter_type(conn):
    """encounter_type CHECK constraint must reject an invalid category."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO fact_encounter
                (encounter_id, patient_key, provider_key, facility_key, admit_date_key,
                 discharge_date_key, encounter_type, length_of_stay_days, total_charge_usd,
                 load_batch_id, loaded_at)
            VALUES ('BAD_ENC_2', 1, 1, 1, 20240101, 20240101, 'NotARealType', 0, 100.0, 1, '2024-01-01')
        """)
        conn.commit()


def test_check_constraint_rejects_negative_charge(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO fact_encounter
                (encounter_id, patient_key, provider_key, facility_key, admit_date_key,
                 discharge_date_key, encounter_type, length_of_stay_days, total_charge_usd,
                 load_batch_id, loaded_at)
            VALUES ('BAD_ENC_3', 1, 1, 1, 20240101, 20240101, 'Outpatient', 0, -50.0, 1, '2024-01-01')
        """)
        conn.commit()


def test_scd2_update_preserves_history(conn):
    """Updating a patient's demographics must expire the old row (is_current=0,
    expiration_date set) and create a new current row with an incremented
    scd_version, while the old row remains queryable for historical facts."""
    pid = "MRN1000005"
    before = conn.execute(
        "SELECT patient_key, scd_version, zip_code FROM dim_patient WHERE patient_id=? AND is_current=1",
        (pid,)).fetchone()
    old_key, old_version, old_zip = before

    new_attrs = {
        "patient_id": pid, "first_name": "Updated", "last_name": "Person",
        "date_of_birth": "1960-01-01", "sex": "F", "race": "White",
        "ethnicity": "Not Hispanic or Latino", "zip_code": "99999",
        "state": "NY", "payer_type": "Medicare",
    }
    batch_id = start_batch(conn, "INCREMENTAL")
    status, new_key = upsert_patient_scd2(conn, new_attrs, batch_id, "2025-01-01")
    conn.commit()
    finish_batch(conn, batch_id, 0, 1)

    assert status == "updated"
    assert new_key != old_key

    old_row = conn.execute("SELECT is_current, expiration_date FROM dim_patient WHERE patient_key=?",
                            (old_key,)).fetchone()
    assert old_row[0] == 0
    assert old_row[1] == "2025-01-01"

    new_row = conn.execute(
        "SELECT is_current, scd_version, zip_code FROM dim_patient WHERE patient_key=?", (new_key,)).fetchone()
    assert new_row[0] == 1
    assert new_row[1] == old_version + 1
    assert new_row[2] == "99999"

    # exactly one current row for this patient_id
    current_count = conn.execute(
        "SELECT COUNT(*) FROM dim_patient WHERE patient_id=? AND is_current=1", (pid,)).fetchone()[0]
    assert current_count == 1

    # both versions still exist (history preserved)
    total_versions = conn.execute(
        "SELECT COUNT(*) FROM dim_patient WHERE patient_id=?", (pid,)).fetchone()[0]
    assert total_versions >= 2


def test_scd2_unchanged_attrs_do_not_create_new_version(conn):
    pid = "MRN1000010"
    row = conn.execute(
        "SELECT * FROM dim_patient WHERE patient_id=? AND is_current=1", (pid,))
    cols = [d[0] for d in row.description]
    current = dict(zip(cols, row.fetchone()))

    batch_id = start_batch(conn, "INCREMENTAL")
    status, key = upsert_patient_scd2(conn, current, batch_id, "2025-02-01")
    conn.commit()
    finish_batch(conn, batch_id, 0, 0)

    assert status == "unchanged"
    assert key == current["patient_key"]


def test_validation_suite_passes_on_clean_database(conn):
    results = run_all_checks(conn)
    failed = [r for r in results if not r.passed]
    assert not failed, f"unexpected validation failures: {[r.check_id for r in failed]}"
    assert len(results) == 13


def test_validation_suite_catches_injected_orphan_row(conn):
    """Inject a data-quality issue by disabling FK enforcement and inserting
    an orphaned fact_lab_result row directly, then confirm VQ04 flips to FAIL
    -- proving the validation query actually detects real problems, not just
    passes trivially."""
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        INSERT INTO fact_lab_result
            (fact_lab_key, encounter_key, patient_key, lab_test_key, date_key, result_value, abnormal_flag)
        VALUES (999999, 1, 1, 9999999, 20240101, 5.0, 'N')
    """)
    conn.commit()

    results = {r.check_id: r for r in run_all_checks(conn)}
    assert results["VQ04_orphan_lab_fact"].passed is False
    assert results["VQ04_orphan_lab_fact"].failing_row_count == 1

    # clean up the injected row and confirm the check passes again
    conn.execute("DELETE FROM fact_lab_result WHERE fact_lab_key=999999")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    results_after = {r.check_id: r for r in run_all_checks(conn)}
    assert results_after["VQ04_orphan_lab_fact"].passed is True


def test_validation_suite_catches_injected_duplicate(conn):
    """Inject a duplicate (encounter_key, lab_test_key, date_key) lab result row
    -- nothing in the schema's constraints prevents this kind of duplicate load
    -- and confirm VQ10 detects it via SQL GROUP BY/HAVING."""
    row = conn.execute("SELECT * FROM fact_lab_result LIMIT 1")
    cols = [d[0] for d in row.description]
    original = dict(zip(cols, row.fetchone()))

    conn.execute("""
        INSERT INTO fact_lab_result
            (encounter_key, patient_key, lab_test_key, date_key, result_value, abnormal_flag)
        VALUES (?,?,?,?,?,?)
    """, (original["encounter_key"], original["patient_key"], original["lab_test_key"],
          original["date_key"], original["result_value"], original["abnormal_flag"]))
    conn.commit()

    results = {r.check_id: r for r in run_all_checks(conn)}
    assert results["VQ10_duplicate_lab_result_rows"].passed is False

    conn.execute("DELETE FROM fact_lab_result WHERE fact_lab_key = (SELECT MAX(fact_lab_key) FROM fact_lab_result)")
    conn.commit()

    results_after = {r.check_id: r for r in run_all_checks(conn)}
    assert results_after["VQ10_duplicate_lab_result_rows"].passed is True
