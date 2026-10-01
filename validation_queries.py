"""
validation_queries.py

Real SQL-based data-quality / data-completeness validation suite, run
against the built SQLite warehouse. Each check is an actual SQL query
(not a hand-rolled Python loop over rows) and returns the offending rows
so failures are actionable.

Categories covered:
  VQ01-VQ05  Referential integrity (orphaned fact rows against each dimension)
  VQ06       SCD2 integrity (exactly one is_current=1 row per patient_id)
  VQ07-VQ09  Required-field / NOT NULL-equivalent checks (empty strings, blanks)
  VQ10       Duplicate detection (duplicate natural keys)
  VQ11-VQ12  Value-range checks (negative charges, lab result value sanity)
  VQ13       Date-logic check (discharge before admit)
"""
import sqlite3
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    check_id: str
    description: str
    passed: bool
    failing_row_count: int
    sample_rows: list = field(default_factory=list)


CHECKS = {
    "VQ01_orphan_encounter_patient": (
        "Every fact_encounter.patient_key must exist in dim_patient",
        """SELECT fe.encounter_key, fe.patient_key FROM fact_encounter fe
           LEFT JOIN dim_patient dp ON fe.patient_key = dp.patient_key
           WHERE dp.patient_key IS NULL"""
    ),
    "VQ02_orphan_diagnosis_fact": (
        "Every fact_diagnosis.encounter_key must exist in fact_encounter",
        """SELECT fd.fact_diagnosis_key FROM fact_diagnosis fd
           LEFT JOIN fact_encounter fe ON fd.encounter_key = fe.encounter_key
           WHERE fe.encounter_key IS NULL"""
    ),
    "VQ03_orphan_procedure_fact": (
        "Every fact_procedure.procedure_key must exist in dim_procedure",
        """SELECT fp.fact_procedure_key FROM fact_procedure fp
           LEFT JOIN dim_procedure dpr ON fp.procedure_key = dpr.procedure_key
           WHERE dpr.procedure_key IS NULL"""
    ),
    "VQ04_orphan_lab_fact": (
        "Every fact_lab_result.lab_test_key must exist in dim_lab_test",
        """SELECT fl.fact_lab_key FROM fact_lab_result fl
           LEFT JOIN dim_lab_test dl ON fl.lab_test_key = dl.lab_test_key
           WHERE dl.lab_test_key IS NULL"""
    ),
    "VQ05_orphan_medication_fact": (
        "Every fact_medication_order.medication_key must exist in dim_medication",
        """SELECT fm.fact_medication_key FROM fact_medication_order fm
           LEFT JOIN dim_medication dm ON fm.medication_key = dm.medication_key
           WHERE dm.medication_key IS NULL"""
    ),
    "VQ06_scd2_single_current_version": (
        "Each patient_id must have exactly one is_current=1 row in dim_patient",
        """SELECT patient_id, COUNT(*) AS current_versions FROM dim_patient
           WHERE is_current = 1
           GROUP BY patient_id
           HAVING COUNT(*) <> 1"""
    ),
    "VQ07_required_field_patient_name": (
        "dim_patient.first_name/last_name must not be null or blank",
        """SELECT patient_key FROM dim_patient
           WHERE first_name IS NULL OR TRIM(first_name) = ''
              OR last_name IS NULL OR TRIM(last_name) = ''"""
    ),
    "VQ08_required_field_encounter_type": (
        "fact_encounter.encounter_type must not be null or blank",
        """SELECT encounter_key FROM fact_encounter
           WHERE encounter_type IS NULL OR TRIM(encounter_type) = ''"""
    ),
    "VQ09_required_field_diagnosis_code": (
        "dim_diagnosis.icd10_code must not be null or blank",
        """SELECT diagnosis_key FROM dim_diagnosis
           WHERE icd10_code IS NULL OR TRIM(icd10_code) = ''"""
    ),
    "VQ10_duplicate_lab_result_rows": (
        "fact_lab_result must not contain more than one result row for the same "
        "encounter + lab test + date (duplicate-load detection)",
        """SELECT encounter_key, lab_test_key, date_key, COUNT(*) AS n FROM fact_lab_result
           GROUP BY encounter_key, lab_test_key, date_key HAVING COUNT(*) > 1"""
    ),
    "VQ11_negative_charges": (
        "fact_encounter.total_charge_usd and fact_procedure.charge_amount_usd must be >= 0",
        """SELECT 'encounter' AS src, encounter_key AS id, total_charge_usd AS amount
           FROM fact_encounter WHERE total_charge_usd < 0
           UNION ALL
           SELECT 'procedure', fact_procedure_key, charge_amount_usd
           FROM fact_procedure WHERE charge_amount_usd < 0"""
    ),
    "VQ12_lab_result_out_of_plausible_bounds": (
        "fact_lab_result.result_value must be within a plausible (non-negative, "
        "<10x reference_high) range for its lab test",
        """SELECT fl.fact_lab_key, fl.result_value, dl.test_name, dl.reference_high
           FROM fact_lab_result fl
           JOIN dim_lab_test dl ON fl.lab_test_key = dl.lab_test_key
           WHERE fl.result_value < 0 OR fl.result_value > dl.reference_high * 10"""
    ),
    "VQ13_discharge_before_admit": (
        "fact_encounter.discharge_date_key must not be before admit_date_key",
        """SELECT encounter_key, admit_date_key, discharge_date_key FROM fact_encounter
           WHERE discharge_date_key IS NOT NULL AND discharge_date_key < admit_date_key"""
    ),
}


def run_all_checks(conn: sqlite3.Connection, sample_limit: int = 5):
    results = []
    for check_id, (description, sql) in CHECKS.items():
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        sample = [dict(zip(cols, r)) for r in rows[:sample_limit]]
        results.append(ValidationResult(
            check_id=check_id,
            description=description,
            passed=(len(rows) == 0),
            failing_row_count=len(rows),
            sample_rows=sample,
        ))
    return results


def print_report(results):
    passed = sum(1 for r in results if r.passed)
    print(f"Validation report: {passed}/{len(results)} checks passed\n")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.check_id}: {r.description} (failing rows: {r.failing_row_count})")
        if not r.passed:
            for s in r.sample_rows:
                print(f"    {s}")
    return results


if __name__ == "__main__":
    import os
    db_path = os.path.join(os.path.dirname(__file__), "output", "analytics.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    results = run_all_checks(conn)
    print_report(results)
    conn.close()
