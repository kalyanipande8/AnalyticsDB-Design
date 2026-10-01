"""run_all.py -- builds the warehouse end to end and runs the validation suite."""
import os
import sqlite3

from build_database import build, DB_PATH
from validation_queries import run_all_checks, print_report


def main():
    print("=" * 60)
    print("Step 1/2: Building and loading analytics.db")
    print("=" * 60)
    summary = build()
    for k, v in summary.items():
        print(f"  {k}: {v}")

    print()
    print("=" * 60)
    print("Step 2/2: Running SQL-based validation suite")
    print("=" * 60)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    results = print_report(run_all_checks(conn))
    conn.close()

    failed = [r for r in results if not r.passed]
    if failed:
        raise SystemExit(f"{len(failed)} validation check(s) failed -- see report above.")
    print("\nAll validation checks passed.")


if __name__ == "__main__":
    main()
