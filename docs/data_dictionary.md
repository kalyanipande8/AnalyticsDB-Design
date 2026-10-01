# Data Dictionary — AnalyticsDB-Design

Full column-level reference for every table in `output/analytics.db`.
Schema source of truth: `sql/schema.sql`. All tables use `PRAGMA foreign_keys
= ON`; CHECK constraints are enforced by SQLite at insert time, not just in
application code.

## Dimension tables

### dim_date
Conformed date dimension shared by every fact table.

| Column | Type | Description |
|---|---|---|
| date_key | INTEGER PK | Surrogate key, `YYYYMMDD` integer form |
| full_date | TEXT | ISO date string, unique |
| year | INTEGER | Calendar year |
| quarter | INTEGER | 1-4 |
| month | INTEGER | 1-12 |
| day | INTEGER | Day of month |
| day_of_week | TEXT | Full weekday name |
| is_weekend | INTEGER | 1 if Sat/Sun, else 0 |

### dim_patient (Type-2 Slowly Changing Dimension)
One row per patient *version*. `patient_id` is the stable business key
(MRN); `patient_key` is the surrogate key fact tables reference, so
historical fact rows keep pointing at the demographic snapshot that was
true when the event happened, even after a later address/payer change.

| Column | Type | Description |
|---|---|---|
| patient_key | INTEGER PK | Surrogate key, referenced by all fact tables |
| patient_id | TEXT | Natural/business key (MRN); repeats across versions |
| first_name, last_name | TEXT | Patient name |
| date_of_birth | TEXT | ISO date |
| sex | TEXT | F / M / U |
| race | TEXT | OMB race category |
| ethnicity | TEXT | Hispanic/Latino or not |
| zip_code | TEXT | 5-digit ZIP |
| state | TEXT | 2-letter state code |
| payer_type | TEXT | Medicare / Medicaid / Commercial / SelfPay / Other |
| effective_date | TEXT | Date this version became current |
| expiration_date | TEXT (nullable) | Date this version was superseded; NULL = still current |
| is_current | INTEGER | 1 = current version. Enforced unique per patient_id via `ux_dim_patient_current` |
| scd_version | INTEGER | 1, 2, 3... increments on each demographic change |
| row_hash | TEXT | SHA-256 of tracked attributes; used to detect whether an incoming record is a real change |
| load_batch_id | INTEGER | FK-like reference to `load_batch.load_batch_id` that created this version |
| loaded_at | TEXT | UTC timestamp of load |

**Lineage:** populated by `build_database.populate_patients()` ->
`upsert_patient_scd2()`. The initial batch inserts every patient as
version 1. A later incremental batch re-runs ~15% of patients with changed
zip/state/payer through the same function, which expires the old row and
inserts a new current version — this is the real "database update /
transformation" operation, not a destructive overwrite.

### dim_provider
| Column | Type | Description |
|---|---|---|
| provider_key | INTEGER PK | Surrogate key |
| provider_id | TEXT | Natural key, unique |
| npi | TEXT | National Provider Identifier, unique |
| full_name | TEXT | Provider display name |
| specialty | TEXT | Clinical specialty |

### dim_facility
| Column | Type | Description |
|---|---|---|
| facility_key | INTEGER PK | Surrogate key |
| facility_id | TEXT | Natural key, unique |
| facility_name | TEXT | Facility display name |
| facility_type | TEXT | Hospital / Clinic / UrgentCare / SurgeryCenter |
| state | TEXT | 2-letter state code |

### dim_diagnosis
| Column | Type | Description |
|---|---|---|
| diagnosis_key | INTEGER PK | Surrogate key |
| icd10_code | TEXT | ICD-10-CM code, unique |
| description | TEXT | Clinical description |
| category | TEXT | Clinical category grouping (e.g. Cardiovascular) |

### dim_procedure
| Column | Type | Description |
|---|---|---|
| procedure_key | INTEGER PK | Surrogate key |
| cpt_code | TEXT | CPT procedure code, unique |
| description | TEXT | Procedure description |
| category | TEXT | Service category (Surgical, Radiology, ...) |

### dim_medication
| Column | Type | Description |
|---|---|---|
| medication_key | INTEGER PK | Surrogate key |
| ndc_code | TEXT | National Drug Code, unique |
| drug_name | TEXT | Generic drug name |
| drug_class | TEXT | Pharmacologic class |

### dim_lab_test
| Column | Type | Description |
|---|---|---|
| lab_test_key | INTEGER PK | Surrogate key |
| loinc_code | TEXT | LOINC code, unique |
| test_name | TEXT | Lab test name |
| units | TEXT | Unit of measure |
| reference_low / reference_high | REAL | Normal reference range, used by validation and abnormal-flag logic |

## Fact tables

### fact_encounter
Grain: one row per patient visit/encounter.

| Column | Type | Description |
|---|---|---|
| encounter_key | INTEGER PK | Surrogate key |
| encounter_id | TEXT | Natural key, unique (source-system id) |
| patient_key | INTEGER FK -> dim_patient | Patient at time of encounter |
| provider_key | INTEGER FK -> dim_provider | Attending provider |
| facility_key | INTEGER FK -> dim_facility | Facility where encounter occurred |
| admit_date_key | INTEGER FK -> dim_date | Admission/visit date |
| discharge_date_key | INTEGER FK -> dim_date (nullable) | Discharge date |
| encounter_type | TEXT | Inpatient / Outpatient / Emergency / Telehealth |
| length_of_stay_days | INTEGER | Days between admit and discharge |
| total_charge_usd | REAL | Total billed charge, >= 0 |
| load_batch_id | INTEGER | Batch that loaded this row |
| loaded_at | TEXT | UTC load timestamp |

### fact_diagnosis
Grain: one row per diagnosis assigned to an encounter (many-to-many
bridge between fact_encounter and dim_diagnosis).

| Column | Type | Description |
|---|---|---|
| fact_diagnosis_key | INTEGER PK | Surrogate key |
| encounter_key | INTEGER FK -> fact_encounter | Parent encounter |
| patient_key | INTEGER FK -> dim_patient | Denormalized for direct patient-level rollups |
| diagnosis_key | INTEGER FK -> dim_diagnosis | Diagnosis code |
| date_key | INTEGER FK -> dim_date | Date diagnosed |
| diagnosis_rank | INTEGER | 1 = primary diagnosis, 2+ = secondary |
| is_primary | INTEGER | 1 if diagnosis_rank = 1 |

### fact_procedure
Grain: one row per procedure performed during an encounter.

| Column | Type | Description |
|---|---|---|
| fact_procedure_key | INTEGER PK | Surrogate key |
| encounter_key | INTEGER FK -> fact_encounter | Parent encounter |
| patient_key | INTEGER FK -> dim_patient | Denormalized patient reference |
| procedure_key | INTEGER FK -> dim_procedure | Procedure code |
| provider_key | INTEGER FK -> dim_provider | Performing provider |
| date_key | INTEGER FK -> dim_date | Date performed |
| quantity | INTEGER | Units performed, >= 1 |
| charge_amount_usd | REAL | Billed charge, >= 0 |

### fact_lab_result
Grain: one row per lab test result during an encounter.

| Column | Type | Description |
|---|---|---|
| fact_lab_key | INTEGER PK | Surrogate key |
| encounter_key | INTEGER FK -> fact_encounter | Parent encounter |
| patient_key | INTEGER FK -> dim_patient | Denormalized patient reference |
| lab_test_key | INTEGER FK -> dim_lab_test | Lab test performed |
| date_key | INTEGER FK -> dim_date | Date resulted |
| result_value | REAL | Numeric result value |
| abnormal_flag | TEXT | N (normal) / L (low) / H (high), relative to dim_lab_test reference range |

### fact_medication_order
Grain: one row per medication order during an encounter.

| Column | Type | Description |
|---|---|---|
| fact_medication_key | INTEGER PK | Surrogate key |
| encounter_key | INTEGER FK -> fact_encounter | Parent encounter |
| patient_key | INTEGER FK -> dim_patient | Denormalized patient reference |
| medication_key | INTEGER FK -> dim_medication | Medication ordered |
| provider_key | INTEGER FK -> dim_provider | Ordering provider |
| date_key | INTEGER FK -> dim_date | Order date |
| dose | TEXT | Dose text, e.g. "10 mg" |
| route | TEXT | Oral / IV / IM / Topical / Inhaled |

## Load metadata

### load_batch
Tracks every load run (initial or incremental) for auditability.

| Column | Type | Description |
|---|---|---|
| load_batch_id | INTEGER PK | Surrogate key |
| batch_type | TEXT | INITIAL or INCREMENTAL |
| started_at / completed_at | TEXT | UTC timestamps |
| rows_inserted / rows_updated | INTEGER | Counts for this batch |
| status | TEXT | RUNNING / SUCCESS / FAILED |
