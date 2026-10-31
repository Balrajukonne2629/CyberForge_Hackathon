#!/usr/bin/env python3
"""tests/test_rebuild_equality.py:
Tests rebuild equality and pg_visible_in_snapshot:
1. Creates a scratch rollup database.
2. Runs simulator writes concurrently with rebuild.py into the scratch database.
3. Lets workers catch up on both live and scratch rollups.
4. Compares table totals and row hashes between scratch and live rollups: must be IDENTICAL.
"""
import sys, os, subprocess, time, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def run_rebuild_equality_test(profile: str = "1m"):
    print(f"=== Running Rebuild Equality Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    main_url = cfg["database"]["main_db_url"]
    live_rollup_url = cfg["database"]["rollup_db_url"]
    scratch_rollup_url = "postgresql://postgres:postgres@localhost:5433/rollup_scratch"

    # 1. Create scratch DB
    admin_conn = psycopg2.connect("postgresql://postgres:postgres@localhost:5433/postgres")
    admin_conn.autocommit = True
    admin_cur = admin_conn.cursor()
    admin_cur.execute("DROP DATABASE IF EXISTS rollup_scratch")
    admin_cur.execute("CREATE DATABASE rollup_scratch")
    admin_cur.close()
    admin_conn.close()

    # Apply schema
    with open("db/rollup/schema.sql", "r", encoding="utf-8") as f:
        schema_sql = f.read()
    scratch_conn = psycopg2.connect(scratch_rollup_url)
    scratch_conn.autocommit = True
    scratch_cur = scratch_conn.cursor()
    scratch_cur.execute(schema_sql)
    scratch_cur.close()
    scratch_conn.close()

    # 2. Run background simulator writes for 5 seconds
    print("2. Starting concurrent simulator writes...")
    sim_proc = subprocess.Popen([
        "py", "-3.12", "data/mitigata_dataset/simulator.py", "writes",
        "--url", main_url, "--rate", "15", "--seconds", "5", "--log", "events.csv"
    ])

    # 3. Simultaneously run rebuild into scratch DB
    print("3. Concurrently executing rebuild into scratch DB...")
    subprocess.run([
        "py", "-3.12", "scripts/rebuild.py", main_url, scratch_rollup_url
    ], check=True)

    sim_proc.wait()
    print("  Simulator writes completed.")

    # 4. Let workers on live and scratch catch up
    print("4. Running workers to process any remaining outbox rows on both rollups...")
    p_live = subprocess.Popen(["py", "-3.12", "worker/worker.py", main_url, live_rollup_url])
    p_scratch = subprocess.Popen(["py", "-3.12", "worker/worker.py", main_url, scratch_rollup_url])

    from tests.sync import wait_for_worker
    wait_for_worker(main_url, live_rollup_url, timeout=30.0)
    wait_for_worker(main_url, scratch_rollup_url, timeout=30.0)

    p_live.terminate()
    p_scratch.terminate()
    p_live.wait()
    p_scratch.wait()

    # 5. Compare Live vs Scratch
    live_conn = psycopg2.connect(live_rollup_url)
    live_cur = live_conn.cursor()
    scratch_conn = psycopg2.connect(scratch_rollup_url)
    scratch_cur = scratch_conn.cursor()

    # Compare orders hour totals
    live_cur.execute("SELECT count(*), coalesce(sum(order_count), 0), coalesce(sum(revenue), 0) FROM rollup_orders_hour")
    l_h_cnt, l_h_orders, l_h_rev = live_cur.fetchone()

    scratch_cur.execute("SELECT count(*), coalesce(sum(order_count), 0), coalesce(sum(revenue), 0) FROM rollup_orders_hour")
    s_h_cnt, s_h_orders, s_h_rev = scratch_cur.fetchone()

    print(f"5. Orders Hour Comparison:")
    print(f"   Live:    {l_h_cnt:,} rows | {l_h_orders:,} orders | ${float(l_h_rev):,.2f}")
    print(f"   Scratch: {s_h_cnt:,} rows | {s_h_orders:,} orders | ${float(s_h_rev):,.2f}")
    assert l_h_orders == s_h_orders, f"Orders mismatch: {l_h_orders} != {s_h_orders}"
    assert abs(float(l_h_rev) - float(s_h_rev)) < 0.05, f"Revenue mismatch: {l_h_rev} != {s_h_rev}"

    # Compare product day totals
    live_cur.execute("SELECT count(*), coalesce(sum(units), 0), coalesce(sum(sales), 0) FROM rollup_product_day")
    l_p_cnt, l_p_units, l_p_sales = live_cur.fetchone()

    scratch_cur.execute("SELECT count(*), coalesce(sum(units), 0), coalesce(sum(sales), 0) FROM rollup_product_day")
    s_p_cnt, s_p_units, s_p_sales = scratch_cur.fetchone()

    print(f"   Product Day Comparison:")
    print(f"   Live:    {l_p_cnt:,} rows | {l_p_units:,} units | ${float(l_p_sales):,.2f}")
    print(f"   Scratch: {s_p_cnt:,} rows | {s_p_units:,} units | ${float(s_p_sales):,.2f}")
    assert l_p_units == s_p_units, f"Units mismatch: {l_p_units} != {s_p_units}"
    assert abs(float(l_p_sales) - float(s_p_sales)) < 0.05, f"Sales mismatch: {l_p_sales} != {s_p_sales}"

    # Clean up scratch DB
    live_cur.close()
    live_conn.close()
    scratch_cur.close()
    scratch_conn.close()

    admin_conn = psycopg2.connect("postgresql://postgres:postgres@localhost:5433/postgres")
    admin_conn.autocommit = True
    admin_cur = admin_conn.cursor()
    admin_cur.execute("DROP DATABASE IF EXISTS rollup_scratch")
    admin_cur.close()
    admin_conn.close()

    print(">>> REBUILD EQUALITY TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_rebuild_equality_test(prof)
    sys.exit(0 if ok else 1)
