# Metadata Catalog — AnalyticsDB-Design

This catalog documents the warehouse at the system level: lineage,
refresh cadence, ownership conventions, and the quality controls attached
to each table. Column-level detail lives in `docs/data_dictionary.md`.

## System overview

| Property | Value |
|---|---|
| Engine | SQLite 3 |
| File | `output/analytics.db` |
| Schema definition | `sql/schema.sql` |
| Build/load entry point | `build_database.py` (`build()`) |
| Validation entry point | `validation_queries.py` (`run_all_checks()`) |
| Orchestration | `run_all.py` |
| Design pattern | Star/snowflake schema: 8 dimension tables, 5 fact tables, 1 load-audit table |
| Random seed | 42 (fixed, for reproducible simulated data) |

## Table catalog

| Table | Type | Grain | Row source | Refresh pattern |
|---|---|---|---|---|
| dim_date | Dimension (conformed) | 1 row/calendar day | Generated range 2023-06-01 to 2025-01-15 | Static, generated once |
| dim_patient | Dimension (Type-2 SCD) | 1 row/patient *version* | `populate_patients()` | INITIAL load + INCREMENTAL SCD2 upserts |
| dim_provider | Dimension | 1 row/provider | `populate_reference_dims()` | Static reference load |
| dim_facility | Dimension | 1 row/facility | `populate_reference_dims()` | Static reference load |
| dim_diagnosis | Dimension | 1 row/ICD-10 code | `populate_reference_dims()` | Static reference load |
| dim_procedure | Dimension | 1 row/CPT code | `populate_reference_dims()` | Static reference load |
| dim_medication | Dimension | 1 row/NDC code | `populate_reference_dims()` | Static reference load |
| dim_lab_test | Dimension | 1 row/LOINC code | `populate_reference_dims()` | Static reference load |
| fact_encounter | Fact | 1 row/encounter | `load_fact_batch()` | INCREMENTAL, idempotent on `encounter_id` |
| fact_diagnosis | Fact (bridge) | 1 row/diagnosis-per-encounter | `load_fact_batch()` | Loaded with parent encounter |
| fact_procedure | Fact | 1 row/procedure-per-encounter | `load_fact_batch()` | Loaded with parent encounter |
| fact_lab_result | Fact | 1 row/lab result-per-encounter | `load_fact_batch()` | Loaded with parent encounter |
| fact_medication_order | Fact | 1 row/medication order-per-encounter | `load_fact_batch()` | Loaded with parent encounter |
| load_batch | Audit/metadata | 1 row/load run | `start_batch()` / `finish_batch()` | Written by every load operation |

## Lineage

```
sql/schema.sql  --(DDL)-->  output/analytics.db (empty, constrained)

build_database.populate_dim_date / populate_reference_dims
    --(static reference data)-->  dim_date, dim_provider, dim_facility,
                                   dim_diagnosis, dim_procedure,
                                   dim_medication, dim_lab_test

build_database.populate_patients --(INITIAL batch)--> dim_patient (v1 rows, is_current=1)
build_database.populate_patients --(INCREMENTAL batch, ~15% of patients)
    --> upsert_patient_scd2() --> dim_patient (old row expired, new v2 row is_current=1)

build_database.load_fact_batch --(INCREMENTAL, idempotent on encounter_id)
    --> fact_encounter, fact_diagnosis, fact_procedure, fact_lab_result,
        fact_medication_order

validation_queries.run_all_checks --(reads)--> all tables above
    --(writes)--> console report (VQ01-VQ13); no table mutation
```

Every load operation (`populate_patients`, `load_fact_batch`) records a row
in `load_batch` with `batch_type`, row counts, and status, so any run of
the pipeline is independently auditable after the fact by querying
`load_batch` rather than trusting console output alone.

## Data-quality controls attached to this catalog

| Control type | Mechanism | Where |
|---|---|---|
| Referential integrity | `PRAGMA foreign_keys = ON` + FK clauses | `sql/schema.sql`, checked live by SQLite on every insert |
| Domain/value constraints | CHECK constraints (enum-like fields, non-negative amounts, valid ranges) | `sql/schema.sql` |
| Single-current-version rule | Partial UNIQUE index `ux_dim_patient_current` (`WHERE is_current = 1`) | `sql/schema.sql` |
| Referential integrity (post-hoc) | VQ01-VQ05 | `validation_queries.py` |
| SCD2 integrity | VQ06 | `validation_queries.py` |
| Required-field completeness | VQ07-VQ09 | `validation_queries.py` |
| Duplicate-load detection | VQ10 | `validation_queries.py` |
| Value-range plausibility | VQ11-VQ12 | `validation_queries.py` |
| Temporal logic | VQ13 | `validation_queries.py` |
| Constraint-rejection + SCD2 + validation-detection regression tests | `tests/test_pipeline.py` | run via `pytest -q` |

## Ownership & conventions

- All simulated data is generated with Python's `random.Random(42)` — no
  real patient, provider, or facility data is used anywhere in this
  repository.
- Surrogate keys (`*_key`, `INTEGER PRIMARY KEY AUTOINCREMENT`) are always
  used for fact-table foreign keys, never natural/business keys, so that
  SCD changes and source-system id churn never invalidate existing fact
  rows.
- Natural/business keys (`patient_id`, `encounter_id`, `icd10_code`, `npi`,
  etc.) are retained as `UNIQUE`-constrained columns for traceability back
  to source systems and for incremental-load idempotency checks.
