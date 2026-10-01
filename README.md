# AnalyticsDB-Design

Analytics-ready relational database design for healthcare analytics: a
normalized star/snowflake schema, a real incremental-load and Type-2
slowly-changing-dimension (SCD2) update mechanism, and a SQL-based data
validation suite — built and run against a real SQLite database.

> **Disclaimer:** This is a portfolio/demo project. All patients,
> encounters, diagnoses, procedures, labs, and medications are
> **synthetically simulated** by `build_database.py` with a fixed random
> seed (42). No real patient, provider, or facility data is used. The
> relational design, SCD2 update mechanism, incremental-load pattern, and
> SQL validation checks are real and are actually implemented and
> executed — only the underlying healthcare data is fictional.

## What it does

1. **`sql/schema.sql`** defines a star/snowflake schema optimized for
   analytical querying, built for real into a SQLite database
   (`output/analytics.db`): 8 dimension tables (`dim_date`, `dim_patient`
   [Type-2 SCD], `dim_provider`, `dim_facility`, `dim_diagnosis`,
   `dim_procedure`, `dim_medication`, `dim_lab_test`) and 5 fact tables
   (`fact_encounter`, `fact_diagnosis`, `fact_procedure`,
   `fact_lab_result`, `fact_medication_order`), plus a `load_batch` audit
   table — 14 tables total, real foreign keys and CHECK constraints,
   `PRAGMA foreign_keys = ON` enforced.

2. **`build_database.py`** populates it with realistic simulated data and
   implements two real "database update / transformation" mechanisms:
   - **SCD2 patient dimension updates**: an INITIAL batch loads all
     patients as version 1; a later INCREMENTAL batch applies real
     demographic changes (address/payer) to ~15% of patients through
     `upsert_patient_scd2()`, which expires the old row and inserts a new
     current version — full history is preserved, not overwritten.
   - **Idempotent incremental fact load**: `load_fact_batch()` is keyed on
     the natural `encounter_id` and skips already-loaded encounters, so
     re-running it is safe (demonstrated by running it twice in `build()`).

3. **`validation_queries.py`** runs 13 real SQL-based validation checks
   (`VQ01`-`VQ13`) against the live database: referential-integrity checks
   (orphaned fact rows across every dimension), SCD2 single-current-version
   integrity, required-field/completeness checks, duplicate-load
   detection, value-range/plausibility checks, and encounter date-logic
   checks.

4. **`docs/data_dictionary.md`** and **`docs/metadata_catalog.md`**
   document every table and column (type, description, lineage) and the
   system-level lineage/refresh/ownership conventions.

5. **`run_all.py`** runs the whole pipeline end to end: build + load, then
   validate, and fails loudly if any validation check fails.

## Entity-Relationship overview

```
dim_date ---< fact_encounter >--- dim_patient (SCD2), dim_provider, dim_facility
fact_encounter ---< fact_diagnosis >--- dim_diagnosis
fact_encounter ---< fact_procedure >--- dim_procedure, dim_provider
fact_encounter ---< fact_lab_result >--- dim_lab_test
fact_encounter ---< fact_medication_order >--- dim_medication, dim_provider

load_batch(load_batch_id) -> referenced by dim_patient.load_batch_id,
                              fact_encounter.load_batch_id (load auditability)
```

Normalization notes: reference dimensions (diagnosis, procedure,
medication, lab test, provider, facility) are kept in 3NF with natural
keys exposed as `UNIQUE` columns for source traceability. `dim_patient`
intentionally departs from strict normalization via Type-2 SCD
versioning — a standard data-warehouse pattern that trades some
redundancy for correct point-in-time analytics (a fact row always
resolves to the demographic snapshot that was true when the clinical
event happened, even after the patient's address or payer changes
later). Fact tables use surrogate integer keys exclusively, never natural
keys, so SCD changes and source-system id churn never invalidate existing
fact rows. Full column-level detail is in `sql/schema.sql`, which is
heavily commented.

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run_all.py
pytest -q
```

## Results

_All numbers below are copied directly from an actual run of `python
run_all.py` and `pytest -q` on 2026-10-01 — nothing is hardcoded. The
fixed random seed (42) makes them reproducible._

### Database population

| Entity | Count |
|---|---|
| dim_date rows | 595 |
| Patients loaded (INITIAL batch) | 400 |
| Patients SCD2-updated (INCREMENTAL batch) | 60 |
| Encounters loaded | 1,500 |
| Diagnoses recorded | 2,994 |
| Procedures recorded | 1,596 |
| Lab results recorded | 1,764 |
| Medication orders recorded | 1,155 |
| Idempotent re-run of fact loader | 0 new encounters inserted (confirms incremental load is safe to re-run) |

### Validation suite (`python run_all.py` / `validation_queries.py`)

```
Validation report: 13/13 checks passed

[PASS] VQ01_orphan_encounter_patient
[PASS] VQ02_orphan_diagnosis_fact
[PASS] VQ03_orphan_procedure_fact
[PASS] VQ04_orphan_lab_fact
[PASS] VQ05_orphan_medication_fact
[PASS] VQ06_scd2_single_current_version
[PASS] VQ07_required_field_patient_name
[PASS] VQ08_required_field_encounter_type
[PASS] VQ09_required_field_diagnosis_code
[PASS] VQ10_duplicate_lab_result_rows
[PASS] VQ11_negative_charges
[PASS] VQ12_lab_result_out_of_plausible_bounds
[PASS] VQ13_discharge_before_admit

All validation checks passed.
```

### Tests (`pytest -q`)

```
9 passed in 0.29s
```

Test coverage includes: schema constraints genuinely rejecting a bad
insert (`sqlite3.IntegrityError` on FK violation, invalid enum value, and
negative charge), the SCD2 upsert correctly expiring the old patient
version and creating a new current one (history preserved and queryable),
an unchanged-attribute upsert correctly creating no new version, and the
validation suite correctly flipping from PASS to FAIL when an orphaned
row and a duplicate row are injected directly into the database, then
back to PASS once removed.

## Repository layout

```
sql/schema.sql            -- DDL: 14 tables, FKs, CHECK constraints, indexes
build_database.py         -- build/load: dims, SCD2 patient upsert, incremental fact load
validation_queries.py     -- 13 SQL-based data-quality/completeness checks
run_all.py                -- end-to-end orchestration
docs/data_dictionary.md   -- column-level documentation for every table
docs/metadata_catalog.md  -- system-level lineage, refresh cadence, ownership
tests/test_pipeline.py    -- constraint, SCD2, and validation-detection tests
output/analytics.db       -- generated SQLite database (gitignored)
```
