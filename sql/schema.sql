-- AnalyticsDB-Design: Analytics-ready healthcare database schema
-- Star/snowflake schema optimized for OLAP-style analytical querying.
--
-- Design notes:
--  * Dimension tables are denormalized where it helps query simplicity
--    (dim_diagnosis, dim_procedure, dim_medication, dim_lab_test), but
--    dim_patient is modeled as a Type-2 Slowly Changing Dimension (SCD2)
--    so demographic history (e.g. address, name changes) is preserved
--    for point-in-time analytics without destroying prior fact linkage.
--  * All fact tables key on surrogate integer keys (not natural/business
--    keys) so that SCD changes and source-system churn never invalidate
--    existing fact rows -- a core data-warehouse design principle.
--  * dim_date is a conformed date dimension shared across every fact
--    table to support consistent time-series rollups (year/quarter/month).
--  * Foreign keys + CHECK constraints are enforced for real (PRAGMA
--    foreign_keys = ON at the application layer) to keep the warehouse
--    referentially sound as new batches are loaded.

PRAGMA foreign_keys = ON;

-- ============================================================
-- DIMENSION TABLES
-- ============================================================

CREATE TABLE dim_date (
    date_key        INTEGER PRIMARY KEY,   -- YYYYMMDD
    full_date       TEXT NOT NULL UNIQUE,
    year            INTEGER NOT NULL,
    quarter         INTEGER NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    month           INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    day             INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31),
    day_of_week     TEXT NOT NULL,
    is_weekend      INTEGER NOT NULL CHECK (is_weekend IN (0, 1))
);

-- Type-2 Slowly Changing Dimension: one row per patient per demographic
-- "version". patient_id is the stable natural/business key; patient_key
-- is the surrogate key that fact rows reference. Only one row per
-- patient_id may have is_current = 1 at a time (enforced in app code via
-- the upsert_patient_scd2 transaction, and checked by validation query
-- VQ06 and tests/test_pipeline.py).
CREATE TABLE dim_patient (
    patient_key     INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      TEXT NOT NULL,             -- stable business key (MRN)
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    date_of_birth   TEXT NOT NULL,
    sex             TEXT NOT NULL CHECK (sex IN ('F', 'M', 'U')),
    race            TEXT,
    ethnicity       TEXT,
    zip_code        TEXT NOT NULL,
    state           TEXT NOT NULL,
    payer_type      TEXT NOT NULL CHECK (payer_type IN ('Medicare', 'Medicaid', 'Commercial', 'SelfPay', 'Other')),
    effective_date  TEXT NOT NULL,             -- SCD2 validity start
    expiration_date TEXT,                      -- SCD2 validity end (NULL = open)
    is_current      INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    scd_version     INTEGER NOT NULL CHECK (scd_version >= 1),
    row_hash        TEXT NOT NULL,             -- hash of tracked attributes, used to detect real changes
    load_batch_id   INTEGER NOT NULL,
    loaded_at       TEXT NOT NULL
);
CREATE INDEX ix_dim_patient_natural_key ON dim_patient(patient_id);
CREATE UNIQUE INDEX ux_dim_patient_current ON dim_patient(patient_id) WHERE is_current = 1;

CREATE TABLE dim_provider (
    provider_key    INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id     TEXT NOT NULL UNIQUE,
    npi             TEXT NOT NULL UNIQUE,
    full_name       TEXT NOT NULL,
    specialty       TEXT NOT NULL
);

CREATE TABLE dim_facility (
    facility_key    INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id     TEXT NOT NULL UNIQUE,
    facility_name   TEXT NOT NULL,
    facility_type   TEXT NOT NULL CHECK (facility_type IN ('Hospital', 'Clinic', 'UrgentCare', 'SurgeryCenter')),
    state           TEXT NOT NULL
);

CREATE TABLE dim_diagnosis (
    diagnosis_key   INTEGER PRIMARY KEY AUTOINCREMENT,
    icd10_code      TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL,
    category        TEXT NOT NULL
);

CREATE TABLE dim_procedure (
    procedure_key   INTEGER PRIMARY KEY AUTOINCREMENT,
    cpt_code        TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL,
    category        TEXT NOT NULL
);

CREATE TABLE dim_medication (
    medication_key  INTEGER PRIMARY KEY AUTOINCREMENT,
    ndc_code        TEXT NOT NULL UNIQUE,
    drug_name       TEXT NOT NULL,
    drug_class      TEXT NOT NULL
);

CREATE TABLE dim_lab_test (
    lab_test_key    INTEGER PRIMARY KEY AUTOINCREMENT,
    loinc_code      TEXT NOT NULL UNIQUE,
    test_name       TEXT NOT NULL,
    units           TEXT NOT NULL,
    reference_low   REAL NOT NULL,
    reference_high  REAL NOT NULL CHECK (reference_high >= reference_low)
);

-- ============================================================
-- FACT TABLES
-- ============================================================

CREATE TABLE fact_encounter (
    encounter_key       INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_id        TEXT NOT NULL UNIQUE,
    patient_key         INTEGER NOT NULL REFERENCES dim_patient(patient_key),
    provider_key        INTEGER NOT NULL REFERENCES dim_provider(provider_key),
    facility_key        INTEGER NOT NULL REFERENCES dim_facility(facility_key),
    admit_date_key      INTEGER NOT NULL REFERENCES dim_date(date_key),
    discharge_date_key  INTEGER REFERENCES dim_date(date_key),
    encounter_type      TEXT NOT NULL CHECK (encounter_type IN ('Inpatient', 'Outpatient', 'Emergency', 'Telehealth')),
    length_of_stay_days INTEGER NOT NULL CHECK (length_of_stay_days >= 0),
    total_charge_usd    REAL NOT NULL CHECK (total_charge_usd >= 0),
    load_batch_id       INTEGER NOT NULL,
    loaded_at           TEXT NOT NULL
);
CREATE INDEX ix_fact_encounter_patient ON fact_encounter(patient_key);
CREATE INDEX ix_fact_encounter_admit_date ON fact_encounter(admit_date_key);
CREATE INDEX ix_fact_encounter_facility ON fact_encounter(facility_key);

CREATE TABLE fact_diagnosis (
    fact_diagnosis_key  INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_key       INTEGER NOT NULL REFERENCES fact_encounter(encounter_key),
    patient_key         INTEGER NOT NULL REFERENCES dim_patient(patient_key),
    diagnosis_key       INTEGER NOT NULL REFERENCES dim_diagnosis(diagnosis_key),
    date_key            INTEGER NOT NULL REFERENCES dim_date(date_key),
    diagnosis_rank      INTEGER NOT NULL CHECK (diagnosis_rank >= 1),  -- 1 = primary diagnosis
    is_primary          INTEGER NOT NULL CHECK (is_primary IN (0, 1))
);
CREATE INDEX ix_fact_diagnosis_encounter ON fact_diagnosis(encounter_key);
CREATE INDEX ix_fact_diagnosis_patient ON fact_diagnosis(patient_key);
CREATE INDEX ix_fact_diagnosis_diag ON fact_diagnosis(diagnosis_key);

CREATE TABLE fact_procedure (
    fact_procedure_key  INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_key       INTEGER NOT NULL REFERENCES fact_encounter(encounter_key),
    patient_key         INTEGER NOT NULL REFERENCES dim_patient(patient_key),
    procedure_key       INTEGER NOT NULL REFERENCES dim_procedure(procedure_key),
    provider_key        INTEGER NOT NULL REFERENCES dim_provider(provider_key),
    date_key            INTEGER NOT NULL REFERENCES dim_date(date_key),
    quantity             INTEGER NOT NULL CHECK (quantity >= 1),
    charge_amount_usd    REAL NOT NULL CHECK (charge_amount_usd >= 0)
);
CREATE INDEX ix_fact_procedure_encounter ON fact_procedure(encounter_key);
CREATE INDEX ix_fact_procedure_patient ON fact_procedure(patient_key);

CREATE TABLE fact_lab_result (
    fact_lab_key        INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_key       INTEGER NOT NULL REFERENCES fact_encounter(encounter_key),
    patient_key         INTEGER NOT NULL REFERENCES dim_patient(patient_key),
    lab_test_key        INTEGER NOT NULL REFERENCES dim_lab_test(lab_test_key),
    date_key            INTEGER NOT NULL REFERENCES dim_date(date_key),
    result_value         REAL NOT NULL,
    abnormal_flag        TEXT NOT NULL CHECK (abnormal_flag IN ('N', 'L', 'H'))  -- Normal/Low/High
);
CREATE INDEX ix_fact_lab_encounter ON fact_lab_result(encounter_key);
CREATE INDEX ix_fact_lab_patient ON fact_lab_result(patient_key);
CREATE INDEX ix_fact_lab_test ON fact_lab_result(lab_test_key);

CREATE TABLE fact_medication_order (
    fact_medication_key  INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_key        INTEGER NOT NULL REFERENCES fact_encounter(encounter_key),
    patient_key          INTEGER NOT NULL REFERENCES dim_patient(patient_key),
    medication_key        INTEGER NOT NULL REFERENCES dim_medication(medication_key),
    provider_key          INTEGER NOT NULL REFERENCES dim_provider(provider_key),
    date_key              INTEGER NOT NULL REFERENCES dim_date(date_key),
    dose                   TEXT NOT NULL,
    route                  TEXT NOT NULL CHECK (route IN ('Oral', 'IV', 'IM', 'Topical', 'Inhaled'))
);
CREATE INDEX ix_fact_med_encounter ON fact_medication_order(encounter_key);
CREATE INDEX ix_fact_med_patient ON fact_medication_order(patient_key);

-- ============================================================
-- LOAD METADATA / AUDIT TABLE (supports incremental-load tracking)
-- ============================================================
CREATE TABLE load_batch (
    load_batch_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_type      TEXT NOT NULL,       -- 'INITIAL' or 'INCREMENTAL'
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    rows_inserted   INTEGER NOT NULL DEFAULT 0,
    rows_updated    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED'))
);
