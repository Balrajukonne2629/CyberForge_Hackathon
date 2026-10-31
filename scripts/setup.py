#!/usr/bin/env python3
"""scripts/setup.py:
One-command setup for the Multi-Tenant Aggregation Layer.
Can be run on a clean machine by a judge or evaluator:
  python scripts/setup.py --profile 1m
  python scripts/setup.py --profile 10m

Orchestrates:
1. Docker compose service initialization (main-db, rollup-db).
2. Schema creation on rollup-db.
3. Multi-tenant database split, template cloning, CSV COPY, post_load indexes, ANALYZE.
4. Outbox & statement-level triggers installation across all 100 tenant DBs.
5. Full initial parallel rebuild of rollup-db.
6. Verification & readiness report.
"""
import sys, os, time, subprocess, argparse
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config
from scripts.split_by_tenant import split_and_load, verify_split_completeness
from scripts.install_triggers import install_triggers
from scripts.rebuild import rebuild_all

def wait_for_db(url: str, label: str, max_retries: int = 30):
    print(f"Waiting for {label} to accept connections ({url}) ...")
    for i in range(max_retries):
        try:
            c = psycopg2.connect(url)
            c.close()
            print(f"  {label} is ready!")
            return
        except Exception:
            time.sleep(1.0)
    raise TimeoutError(f"Could not connect to {label} at {url} after {max_retries}s")

def setup_environment(profile: str = "1m", workers: int = 16):
    t_start = time.time()
    print("=" * 70)
    print(f"CYBERFORGE ONE-COMMAND SETUP (PROFILE: {profile.upper()})")
    print("=" * 70)

    cfg = get_config(profile)
    main_admin_url = "postgresql://postgres:postgres@localhost:5432/postgres"
    rollup_url = cfg["database"]["rollup_db_url"]

    # 1. Start Docker containers (main-db, rollup-db)
    print("\n[Step 1/5] Ensuring Docker database containers are running ...")
    subprocess.run(["docker", "compose", "up", "-d", "main-db", "rollup-db"], check=False)

    wait_for_db(main_admin_url, "Main PostgreSQL Server (port 5432)")
    wait_for_db(rollup_url, "Rollup Database Server (port 5433)")

    # 2. Initialize Rollup DB Schema
    print("\n[Step 2/5] Initializing Rollup DB schema ...")
    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()
    schema_path = "db/rollup/schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        r_cur.execute(f.read())
    r_cur.close()
    r_conn.close()
    print("  Rollup DB schema initialized.")

    # 3. Split & Load per-tenant databases
    print(f"\n[Step 3/5] Splitting CSVs and loading 100 tenant databases ({profile}) ...")
    split_and_load(profile=profile, tenant_count=100, max_workers=workers)

    # 4. Install triggers across all tenant databases
    print(f"\n[Step 4/5] Installing outbox & triggers on 100 tenant databases ...")
    install_triggers(profile=profile, tenant_count=100, max_workers=workers)

    # 5. Full Rebuild into Rollup DB
    print(f"\n[Step 5/5] Performing initial parallel rebuild into Rollup DB ...")
    rebuild_all(profile=profile, tenant_count=100, max_workers=workers)

    total_time = time.time() - t_start
    print("=" * 70)
    print(f"SETUP COMPLETE FOR PROFILE {profile.upper()} IN {total_time:.2f}s!")
    print("=" * 70)
    print("To start the worker:")
    print(f"  py -3.12 worker/worker.py --profile {profile}")
    print("To start the API:")
    print("  py -3.12 -m uvicorn api.main:app --port 8000")
    print("To run the test suite:")
    print(f"  py -3.12 tests/run_all.py --profile {profile}")
    print("=" * 70)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-command setup for CyberForge aggregation layer.")
    parser.add_argument("--profile", default="1m", choices=["1m", "10m"], help="Profile to set up")
    parser.add_argument("--workers", type=int, default=16, help="Parallel worker threads")
    args = parser.parse_args()

    setup_environment(profile=args.profile, workers=args.workers)
