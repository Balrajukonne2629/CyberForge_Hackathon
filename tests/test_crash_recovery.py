#!/usr/bin/env python3
"""tests/test_crash_recovery.py:
Tests worker crash mid-batch and recovery:
1. Spawns worker.
2. Injects a stream of writes.
3. Abruptly terminates (kills) the worker process while processing.
4. Restarts the worker.
5. Verifies all rollups catch up and match plain SQL ground truth with 0 errors.
"""
import sys, os, subprocess, time, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def run_crash_recovery_test(profile: str = "1m"):
    print(f"=== Running Crash Recovery Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_db_url"]
    excluded = tuple(cfg["business_rules"]["excluded_statuses"])

    # 1. Start worker subprocess
    print("1. Starting worker...")
    p = subprocess.Popen(["py", "-3.12", "worker/worker.py", main_url, rollup_url])
    time.sleep(1.0)

    # 2. Inject rapid bulk mutations
    print("2. Generating active bulk orders on main-db...")
    main_conn = psycopg2.connect(main_url)
    main_conn.autocommit = True
    main_cur = main_conn.cursor()

    base_id = 999_888_000
    # Pre-cleanup in case of aborted previous runs
    main_cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id < %s", (base_id, base_id + 200))
    main_cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id < %s", (base_id, base_id + 200))

    for i in range(50):
        oid = base_id + i
        main_cur.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, 1, 1, 'delivered', 50.00, '2026-10-28 15:00:00', now())
        """, (oid,))
        main_cur.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, 1, 1, 1, 50.00)
        """, (oid, oid))

    # 3. Abruptly KILL the worker process mid-flight
    print("3. Killing worker process abruptly (SIGKILL)...")
    p.kill()
    p.wait()
    print("  Worker killed.")

    # Inject another 50 orders while worker is dead
    print("4. Injecting further orders while worker is offline...")
    for i in range(50, 100):
        oid = base_id + i
        main_cur.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, 1, 1, 'delivered', 50.00, '2026-10-28 15:00:00', now())
        """, (oid,))
        main_cur.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, 1, 1, 1, 50.00)
        """, (oid, oid))

    # 5. Restart worker
    print("5. Restarting worker to recover...")
    p2 = subprocess.Popen(["py", "-3.12", "worker/worker.py", main_url, rollup_url])
    
    from tests.sync import wait_for_worker
    wait_for_worker(main_url, rollup_url, timeout=15.0)

    # 6. Verify ground truth vs rollup
    f_time = "2026-10-28 15:00:00"
    t_time = "2026-10-28 16:00:00"
    main_cur.execute("""
        SELECT count(*), coalesce(sum(total), 0)
        FROM orders
        WHERE tenant_id = 1 AND created_at >= %s AND created_at < %s
          AND status = 'delivered'
    """, (f_time, t_time))
    sql_cnt, sql_rev = main_cur.fetchone()

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_cur = rollup_conn.cursor()
    rollup_cur.execute("""
        SELECT order_count, revenue FROM rollup_orders_hour
        WHERE tenant_id = 1 AND hour = %s AND status = 'delivered'
    """, (f_time,))
    r_cnt, r_rev = rollup_cur.fetchone()

    print(f"6. Verification after crash recovery:")
    print(f"  Main SQL: count={sql_cnt}, rev={float(sql_rev)}")
    print(f"  Rollup:   count={r_cnt}, rev={float(r_rev)}")
    assert sql_cnt == r_cnt
    assert abs(float(sql_rev) - float(r_rev)) < 0.01

    # Cleanup
    for i in range(100):
        oid = base_id + i
        main_cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
        main_cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))

    from tests.sync import wait_for_worker
    wait_for_worker(main_url, rollup_url, timeout=15.0)

    p2.terminate()
    p2.wait()

    main_cur.close()
    main_conn.close()
    rollup_cur.close()
    rollup_conn.close()

    print(">>> CRASH RECOVERY TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_crash_recovery_test(prof)
    sys.exit(0 if ok else 1)
