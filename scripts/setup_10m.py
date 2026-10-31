#!/usr/bin/env python3
"""scripts/setup_10m.py:
Automated end-to-end setup script for the 10M dataset profile:
1. Verifies Docker containers (main-db, rollup-db).
2. Sets up shop_10m and rollup_10m.
3. Applies outbox table and triggers to shop_10m.
4. Applies schema to rollup_10m.
5. Executes rebuild.py into rollup_10m.
6. Reports table sizes and readiness.
"""
import sys, os, time, subprocess, psycopg2

def setup_10m():
    print("=" * 60)
    print("CYBERFORGE 10M PRODUCTION PROFILE SETUP")
    print("=" * 60)

    main_url = "postgresql://postgres:postgres@localhost:5432/shop_10m"
    rollup_url = "postgresql://postgres:postgres@localhost:5433/rollup_10m"

    # 1. Verify Postgres connections
    print("1. Verifying database connectivity...")
    try:
        conn = psycopg2.connect(main_url)
        conn.close()
        print("  Main-DB (shop_10m) connected.")
    except Exception as e:
        print(f"  [ERROR] Cannot connect to main-db shop_10m: {e}")
        return False

    try:
        conn = psycopg2.connect(rollup_url)
        conn.close()
        print("  Rollup-DB (rollup_10m) connected.")
    except Exception as e:
        print(f"  [ERROR] Cannot connect to rollup-db rollup_10m: {e}")
        return False

    # 2. Apply main-db triggers
    print("\n2. Applying outbox table and statement triggers on shop_10m...")
    with open("db/main/triggers.sql", "r", encoding="utf-8") as f:
        trg_sql = f.read()
    conn = psycopg2.connect(main_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(trg_sql)
    cur.close()
    conn.close()
    print("  Outbox and triggers verified on shop_10m.")

    # 3. Apply rollup schema
    print("\n3. Applying rollup schema to rollup_10m...")
    with open("db/rollup/schema.sql", "r", encoding="utf-8") as f:
        schema_sql = f.read()
    conn = psycopg2.connect(rollup_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(schema_sql)
    cur.close()
    conn.close()
    print("  Rollup tables, constraints, and indexes applied on rollup_10m.")

    # 4. Check if rollup is already populated
    conn = psycopg2.connect(rollup_url)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM rollup_orders_hour")
    h_cnt = cur.fetchone()[0]
    cur.close()
    conn.close()

    if h_cnt == 0:
        print("\n4. Populating rollups via rebuild.py (snapshot aggregate)...")
        t0 = time.time()
        subprocess.run(["py", "-3.12", "scripts/rebuild.py", main_url, rollup_url], check=True)
        print(f"  Rebuild completed in {time.time() - t0:.2f}s.")
    else:
        print(f"\n4. Rollup already populated ({h_cnt:,} hours). Skipping full rebuild.")

    # 5. Report sizes
    print("\n5. Reporting storage footprint...")
    subprocess.run(["py", "-3.12", "scripts/report_sizes.py", "10m"], check=True)

    print("\n" + "=" * 60)
    print("10M SETUP COMPLETE AND READY FOR WORKER & API")
    print("=" * 60)
    return True

if __name__ == "__main__":
    ok = setup_10m()
    sys.exit(0 if ok else 1)
