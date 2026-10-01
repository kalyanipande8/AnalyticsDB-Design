"""
build_database.py

Builds the AnalyticsDB-Design SQLite warehouse from sql/schema.sql and
populates it with realistic simulated healthcare data:

  * Reference dimensions (date, provider, facility, diagnosis, procedure,
    medication, lab test) loaded once.
  * dim_patient loaded via a real Type-2 Slowly-Changing-Dimension (SCD2)
    upsert: an INITIAL batch inserts all patients as version 1 / is_current=1,
    then an INCREMENTAL batch re-runs a subset of patients with changed
    demographics (e.g. address/payer change) through `upsert_patient_scd2`,
    which expires the old row and inserts a new current version -- full
    history is preserved, not overwritten.
  * Fact tables (encounter, diagnosis, procedure, lab_result,
    medication_order) are loaded via a real incremental-load mechanism
    keyed on natural ids (`load_fact_batch`) that upserts (insert new /
    skip existing) and records row counts in `load_batch`.

Everything is simulated with Python's `random` module under a fixed seed
(42) so results are reproducible. No real patient data is used.
"""
import hashlib
import os
import random
import sqlite3
from datetime import date, datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "output", "analytics.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "sql", "schema.sql")
SEED = 42

FIRST_NAMES = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
               "William", "Elizabeth", "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
               "Thomas", "Sarah", "Charles", "Karen", "Daniel", "Nancy", "Matthew", "Lisa"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
              "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas"]
STATES = ["NY", "NJ", "CT", "PA", "MA"]
RACES = ["White", "Black or African American", "Asian", "American Indian or Alaska Native",
         "Native Hawaiian or Other Pacific Islander", "Other"]
ETHNICITIES = ["Hispanic or Latino", "Not Hispanic or Latino"]
PAYERS = ["Medicare", "Medicaid", "Commercial", "SelfPay", "Other"]

DIAGNOSES = [
    ("E11.9", "Type 2 diabetes mellitus without complications", "Endocrine"),
    ("I10", "Essential (primary) hypertension", "Cardiovascular"),
    ("J45.909", "Unspecified asthma, uncomplicated", "Respiratory"),
    ("N18.3", "Chronic kidney disease, stage 3", "Renal"),
    ("I25.10", "Atherosclerotic heart disease", "Cardiovascular"),
    ("F32.9", "Major depressive disorder, single episode", "Behavioral Health"),
    ("M54.5", "Low back pain", "Musculoskeletal"),
    ("K21.9", "Gastro-esophageal reflux disease", "Gastrointestinal"),
    ("J18.9", "Pneumonia, unspecified organism", "Respiratory"),
    ("E78.5", "Hyperlipidemia, unspecified", "Endocrine"),
    ("I48.91", "Unspecified atrial fibrillation", "Cardiovascular"),
    ("N39.0", "Urinary tract infection, site not specified", "Renal"),
]

PROCEDURES = [
    ("99213", "Office/outpatient visit, established patient", "Evaluation & Management"),
    ("99284", "Emergency department visit, high severity", "Evaluation & Management"),
    ("80053", "Comprehensive metabolic panel", "Laboratory"),
    ("93000", "Electrocardiogram, routine", "Cardiology"),
    ("71046", "Chest X-ray, 2 views", "Radiology"),
    ("45378", "Colonoscopy, diagnostic", "Surgical"),
    ("29881", "Knee arthroscopy with meniscectomy", "Surgical"),
    ("36415", "Collection of venous blood by venipuncture", "Laboratory"),
    ("93306", "Echocardiogram, complete", "Cardiology"),
    ("97110", "Therapeutic exercise", "Physical Therapy"),
]

MEDICATIONS = [
    ("00071015523", "Metformin", "Antidiabetic"),
    ("00093715601", "Lisinopril", "ACE Inhibitor"),
    ("00172518860", "Albuterol", "Bronchodilator"),
    ("00071015601", "Atorvastatin", "Statin"),
    ("00054827225", "Amoxicillin", "Antibiotic"),
    ("00054031925", "Sertraline", "SSRI"),
    ("00078049715", "Warfarin", "Anticoagulant"),
    ("00006073231", "Losartan", "ARB"),
    ("00093014301", "Omeprazole", "PPI"),
    ("00406357701", "Furosemide", "Diuretic"),
]

LAB_TESTS = [
    ("2345-7", "Glucose", "mg/dL", 70, 99),
    ("2160-0", "Creatinine", "mg/dL", 0.6, 1.3),
    ("2093-3", "Total Cholesterol", "mg/dL", 125, 200),
    ("4548-4", "Hemoglobin A1c", "%", 4.0, 5.6),
    ("718-7", "Hemoglobin", "g/dL", 12.0, 17.5),
    ("2823-3", "Potassium", "mmol/L", 3.5, 5.1),
]

FACILITIES = [
    ("FAC001", "Stony Brook University Hospital", "Hospital", "NY"),
    ("FAC002", "North Shore Family Clinic", "Clinic", "NY"),
    ("FAC003", "Riverside Urgent Care", "UrgentCare", "NJ"),
    ("FAC004", "Eastside Surgery Center", "SurgeryCenter", "CT"),
    ("FAC005", "Metro General Hospital", "Hospital", "PA"),
]

SPECIALTIES = ["Internal Medicine", "Cardiology", "Family Medicine", "Endocrinology",
               "Emergency Medicine", "Nephrology", "Pulmonology", "Orthopedics"]

N_PATIENTS = 400
N_PROVIDERS = 25
N_ENCOUNTERS = 1500


def row_hash(*fields):
    return hashlib.sha256("|".join(str(f) for f in fields).encode()).hexdigest()


def date_key(d):
    return int(d.strftime("%Y%m%d"))


def build_schema(conn):
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())


def populate_dim_date(conn, start, end):
    cur = conn.cursor()
    d = start
    rows = []
    while d <= end:
        rows.append((date_key(d), d.isoformat(), d.year, (d.month - 1) // 3 + 1, d.month, d.day,
                     d.strftime("%A"), 1 if d.weekday() >= 5 else 0))
        d += timedelta(days=1)
    cur.executemany(
        "INSERT INTO dim_date (date_key, full_date, year, quarter, month, day, day_of_week, is_weekend) "
        "VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def populate_reference_dims(conn, rng):
    cur = conn.cursor()
    cur.executemany("INSERT INTO dim_diagnosis (icd10_code, description, category) VALUES (?,?,?)", DIAGNOSES)
    cur.executemany("INSERT INTO dim_procedure (cpt_code, description, category) VALUES (?,?,?)", PROCEDURES)
    cur.executemany("INSERT INTO dim_medication (ndc_code, drug_name, drug_class) VALUES (?,?,?)", MEDICATIONS)
    cur.executemany(
        "INSERT INTO dim_lab_test (loinc_code, test_name, units, reference_low, reference_high) VALUES (?,?,?,?,?)",
        LAB_TESTS)
    cur.executemany("INSERT INTO dim_facility (facility_id, facility_name, facility_type, state) VALUES (?,?,?,?)",
                     FACILITIES)

    providers = []
    for i in range(1, N_PROVIDERS + 1):
        providers.append((f"PRV{i:04d}", f"{1000000000 + i}",
                           f"Dr. {rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                           rng.choice(SPECIALTIES)))
    cur.executemany("INSERT INTO dim_provider (provider_id, npi, full_name, specialty) VALUES (?,?,?,?)", providers)
    conn.commit()


def make_patient_attrs(rng, patient_id):
    return {
        "patient_id": patient_id,
        "first_name": rng.choice(FIRST_NAMES),
        "last_name": rng.choice(LAST_NAMES),
        "date_of_birth": (date(1940, 1, 1) + timedelta(days=rng.randint(0, 30000))).isoformat(),
        "sex": rng.choice(["F", "M"]),
        "race": rng.choice(RACES),
        "ethnicity": rng.choice(ETHNICITIES),
        "zip_code": f"{rng.randint(10000, 19999)}",
        "state": rng.choice(STATES),
        "payer_type": rng.choice(PAYERS),
    }


def start_batch(conn, batch_type):
    cur = conn.cursor()
    cur.execute("INSERT INTO load_batch (batch_type, started_at, status) VALUES (?,?,?)",
                (batch_type, datetime.utcnow().isoformat(), "RUNNING"))
    conn.commit()
    return cur.lastrowid


def finish_batch(conn, batch_id, inserted, updated):
    conn.execute(
        "UPDATE load_batch SET completed_at=?, rows_inserted=?, rows_updated=?, status='SUCCESS' WHERE load_batch_id=?",
        (datetime.utcnow().isoformat(), inserted, updated, batch_id))
    conn.commit()


def upsert_patient_scd2(conn, attrs, batch_id, effective_date):
    """Real Type-2 SCD upsert for dim_patient.

    If the patient_id has no current row -> insert version 1.
    If it has a current row and tracked attributes are unchanged -> no-op.
    If it has a current row and tracked attributes changed -> expire the
    current row (expiration_date = effective_date, is_current = 0) and
    insert a new row (scd_version + 1, is_current = 1). This preserves full
    history: fact rows loaded against the old patient_key still resolve to
    the demographic snapshot that was true when the event occurred.
    Returns ('inserted' | 'updated' | 'unchanged', patient_key).
    """
    tracked = (attrs["first_name"], attrs["last_name"], attrs["date_of_birth"], attrs["sex"],
               attrs["race"], attrs["ethnicity"], attrs["zip_code"], attrs["state"], attrs["payer_type"])
    new_hash = row_hash(*tracked)

    cur = conn.cursor()
    cur.execute(
        "SELECT patient_key, row_hash, scd_version FROM dim_patient WHERE patient_id=? AND is_current=1",
        (attrs["patient_id"],))
    current = cur.fetchone()

    if current is None:
        cur.execute("""
            INSERT INTO dim_patient
                (patient_id, first_name, last_name, date_of_birth, sex, race, ethnicity,
                 zip_code, state, payer_type, effective_date, expiration_date, is_current,
                 scd_version, row_hash, load_batch_id, loaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,1,1,?,?,?)
        """, (attrs["patient_id"], attrs["first_name"], attrs["last_name"], attrs["date_of_birth"],
              attrs["sex"], attrs["race"], attrs["ethnicity"], attrs["zip_code"], attrs["state"],
              attrs["payer_type"], effective_date, new_hash, batch_id, datetime.utcnow().isoformat()))
        return "inserted", cur.lastrowid

    patient_key, old_hash, old_version = current
    if old_hash == new_hash:
        return "unchanged", patient_key

    # Expire old version, insert new current version in one transaction.
    cur.execute("UPDATE dim_patient SET expiration_date=?, is_current=0 WHERE patient_key=?",
                (effective_date, patient_key))
    cur.execute("""
        INSERT INTO dim_patient
            (patient_id, first_name, last_name, date_of_birth, sex, race, ethnicity,
             zip_code, state, payer_type, effective_date, expiration_date, is_current,
             scd_version, row_hash, load_batch_id, loaded_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,1,?,?,?,?)
    """, (attrs["patient_id"], attrs["first_name"], attrs["last_name"], attrs["date_of_birth"],
          attrs["sex"], attrs["race"], attrs["ethnicity"], attrs["zip_code"], attrs["state"],
          attrs["payer_type"], effective_date, old_version + 1, new_hash, batch_id,
          datetime.utcnow().isoformat()))
    return "updated", cur.lastrowid


def populate_patients(conn, rng):
    """INITIAL load of all patients, then an INCREMENTAL batch that applies
    real demographic changes (address + payer change) to a subset, exercised
    through the SCD2 upsert path -- this is the "database update /
    transformation" step."""
    initial_batch = start_batch(conn, "INITIAL")
    inserted = 0
    patient_ids = [f"MRN{1000000 + i}" for i in range(N_PATIENTS)]
    for pid in patient_ids:
        attrs = make_patient_attrs(rng, pid)
        status, _ = upsert_patient_scd2(conn, attrs, initial_batch, "2024-01-01")
        if status == "inserted":
            inserted += 1
    conn.commit()
    finish_batch(conn, initial_batch, inserted, 0)

    # Incremental batch: ~15% of patients get a real demographic change
    # (moved / changed insurance). This exercises the SCD2 "update" path.
    incr_batch = start_batch(conn, "INCREMENTAL")
    changed = 0
    sample = rng.sample(patient_ids, k=int(N_PATIENTS * 0.15))
    for pid in sample:
        cur = conn.execute("SELECT * FROM dim_patient WHERE patient_id=? AND is_current=1", (pid,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        current = dict(zip(cols, row))
        new_attrs = dict(current)
        new_attrs["zip_code"] = f"{rng.randint(10000, 19999)}"
        new_attrs["state"] = rng.choice(STATES)
        new_attrs["payer_type"] = rng.choice(PAYERS)
        status, _ = upsert_patient_scd2(conn, new_attrs, incr_batch, "2024-07-01")
        if status == "updated":
            changed += 1
    conn.commit()
    finish_batch(conn, incr_batch, 0, changed)
    return inserted, changed


def load_fact_batch(conn, rng, start_date, end_date):
    """Real incremental fact load: generates encounters (keyed on a natural
    encounter_id) and upserts them -- re-running this function is idempotent
    because encounter_id is UNIQUE and already-loaded ids are skipped. Child
    fact rows (diagnosis/procedure/lab/medication) are generated per new
    encounter only."""
    batch_id = start_batch(conn, "INCREMENTAL")
    cur = conn.cursor()

    cur.execute("SELECT patient_key, patient_id FROM dim_patient WHERE is_current=1")
    patients = cur.fetchall()
    cur.execute("SELECT provider_key FROM dim_provider")
    providers = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT facility_key FROM dim_facility")
    facilities = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT diagnosis_key FROM dim_diagnosis")
    diagnoses = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT procedure_key FROM dim_procedure")
    procedures = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT medication_key FROM dim_medication")
    medications = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT lab_test_key FROM dim_lab_test")
    lab_tests = [r[0] for r in cur.fetchall()]

    span_days = (end_date - start_date).days
    encounters_inserted = 0
    diag_rows, proc_rows, lab_rows, med_rows = [], [], [], []

    for i in range(N_ENCOUNTERS):
        encounter_id = f"ENC{100000 + i}"
        cur.execute("SELECT 1 FROM fact_encounter WHERE encounter_id=?", (encounter_id,))
        if cur.fetchone():
            continue  # already loaded -- incremental skip

        patient_key, _ = rng.choice(patients)
        provider_key = rng.choice(providers)
        facility_key = rng.choice(facilities)
        admit = start_date + timedelta(days=rng.randint(0, span_days))
        enc_type = rng.choice(["Inpatient", "Outpatient", "Emergency", "Telehealth"])
        los = rng.randint(0, 8) if enc_type == "Inpatient" else 0
        discharge = admit + timedelta(days=los)
        charge = round(rng.uniform(80, 25000), 2)

        cur.execute("""
            INSERT INTO fact_encounter
                (encounter_id, patient_key, provider_key, facility_key, admit_date_key,
                 discharge_date_key, encounter_type, length_of_stay_days, total_charge_usd,
                 load_batch_id, loaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (encounter_id, patient_key, provider_key, facility_key, date_key(admit),
              date_key(discharge), enc_type, los, charge, batch_id, datetime.utcnow().isoformat()))
        encounter_key = cur.lastrowid
        encounters_inserted += 1

        n_diag = rng.randint(1, 3)
        chosen_diag = rng.sample(diagnoses, k=min(n_diag, len(diagnoses)))
        for rank, dkey in enumerate(chosen_diag, start=1):
            diag_rows.append((encounter_key, patient_key, dkey, date_key(admit), rank, 1 if rank == 1 else 0))

        if rng.random() < 0.7:
            for pkey in rng.sample(procedures, k=rng.randint(1, 2)):
                proc_rows.append((encounter_key, patient_key, pkey, provider_key, date_key(admit),
                                   rng.randint(1, 2), round(rng.uniform(40, 5000), 2)))

        if rng.random() < 0.6:
            for lkey in rng.sample(lab_tests, k=rng.randint(1, 3)):
                ref_low, ref_high = [t for t in LAB_TESTS if True][0][3], LAB_TESTS[0][4]
                # look up actual reference range for this lab_test_key
                cur.execute("SELECT reference_low, reference_high FROM dim_lab_test WHERE lab_test_key=?", (lkey,))
                lo, hi = cur.fetchone()
                if rng.random() < 0.85:
                    value = round(rng.uniform(lo, hi), 2)
                    flag = "N"
                elif rng.random() < 0.5:
                    value = round(max(0.01, lo - rng.uniform(0.1, lo * 0.3)), 2)
                    flag = "L"
                else:
                    value = round(hi + rng.uniform(0.1, hi * 0.3 + 1), 2)
                    flag = "H"
                lab_rows.append((encounter_key, patient_key, lkey, date_key(admit), value, flag))

        if rng.random() < 0.5:
            for mkey in rng.sample(medications, k=rng.randint(1, 2)):
                med_rows.append((encounter_key, patient_key, mkey, provider_key, date_key(admit),
                                  f"{rng.choice([5, 10, 20, 25, 50, 100])} mg",
                                  rng.choice(["Oral", "IV", "IM", "Topical", "Inhaled"])))

    cur.executemany("""
        INSERT INTO fact_diagnosis (encounter_key, patient_key, diagnosis_key, date_key, diagnosis_rank, is_primary)
        VALUES (?,?,?,?,?,?)""", diag_rows)
    cur.executemany("""
        INSERT INTO fact_procedure (encounter_key, patient_key, procedure_key, provider_key, date_key, quantity, charge_amount_usd)
        VALUES (?,?,?,?,?,?,?)""", proc_rows)
    cur.executemany("""
        INSERT INTO fact_lab_result (encounter_key, patient_key, lab_test_key, date_key, result_value, abnormal_flag)
        VALUES (?,?,?,?,?,?)""", lab_rows)
    cur.executemany("""
        INSERT INTO fact_medication_order (encounter_key, patient_key, medication_key, provider_key, date_key, dose, route)
        VALUES (?,?,?,?,?,?,?)""", med_rows)

    conn.commit()
    finish_batch(conn, batch_id, encounters_inserted, 0)
    return {
        "encounters": encounters_inserted,
        "diagnoses": len(diag_rows),
        "procedures": len(proc_rows),
        "labs": len(lab_rows),
        "medications": len(med_rows),
    }


def build(db_path=DB_PATH, seed=SEED):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    rng = random.Random(seed)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    build_schema(conn)

    n_dates = populate_dim_date(conn, date(2023, 6, 1), date(2025, 1, 15))
    populate_reference_dims(conn, rng)
    inserted, changed = populate_patients(conn, rng)
    fact_counts = load_fact_batch(conn, rng, date(2024, 1, 1), date(2024, 12, 31))

    # Run the incremental fact loader a second time to demonstrate
    # idempotent incremental load (should insert 0 new encounters).
    rerun_counts = load_fact_batch(conn, rng, date(2024, 1, 1), date(2024, 12, 31))

    summary = {
        "dim_date_rows": n_dates,
        "patients_inserted": inserted,
        "patients_scd_updated": changed,
        "fact_load_run1": fact_counts,
        "fact_load_run2_idempotent_check": rerun_counts,
    }
    conn.close()
    return summary


if __name__ == "__main__":
    result = build()
    print("Database build complete:")
    for k, v in result.items():
        print(f"  {k}: {v}")
