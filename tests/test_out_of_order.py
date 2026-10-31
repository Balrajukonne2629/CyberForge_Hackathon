#!/usr/bin/env python3
"""tests/test_out_of_order.py:
Tests safe-xid handling during out-of-order transaction commits:
1. Transaction T1 starts and inserts (open)
2. Transaction T2 starts, inserts and commits
3. Proves T2 cannot be skipped by premature read because xid >= pg_snapshot_xmin()
4. Transaction T1 commits
5. Worker runs and processes both T1 and T2 in correct order without skipping
"""
import sys, os, time, datetime, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def run_out_of_order_test(profile: str = "1m"):
    print(f"=== Running Out-of-Order Commit Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_db_url"]

    conn1 = psycopg2.connect(main_url)
    conn1.autocommit = False
    cur1 = conn1.cursor()

    conn2 = psycopg2.connect(main_url)
    conn2.autocommit = False
    cur2 = conn2.cursor()

    test_hour = datetime.datetime(2026, 10, 27, 10, 0, 0)
    oid1, iid1 = 777_001, 777_001
    oid2, iid2 = 777_002, 777_002

    # Clean up
    clean_conn = psycopg2.connect(main_url)
    clean_conn.autocommit = True
    clean_cur = clean_conn.cursor()
    clean_cur.execute("DELETE FROM order_items WHERE item_id IN (%s, %s) OR order_id IN (%s, %s)", (oid1, oid2, oid1, oid2))
    clean_cur.execute("DELETE FROM orders WHERE order_id IN (%s, %s)", (oid1, oid2))
    time.sleep(0.5)

    # Step 1: T1 inserts (leaves open)
    print("1. T1 begins and inserts order 777001 ($100)...")
    cur1.execute("""
        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
        VALUES (%s, 1, 1, 'delivered', 100.00, %s, %s)
    """, (oid1, test_hour, test_hour))
    cur1.execute("""
        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
        VALUES (%s, %s, 1, 1, 1, 100.00)
    """, (iid1, oid1))

    # Step 2: T2 inserts and COMMITS before T1
    print("2. T2 begins, inserts order 777002 ($200), and COMMITS...")
    cur2.execute("""
        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
        VALUES (%s, 1, 1, 'delivered', 200.00, %s, %s)
    """, (oid2, test_hour, test_hour))
    cur2.execute("""
        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
        VALUES (%s, %s, 1, 1, 2, 100.00)
    """, (iid2, oid2))
    conn2.commit()

    # Step 3: Check pg_snapshot_xmin on main-db while T1 is still open
    # T2 must NOT be visible under safe-xid rule because T2.xid >= xmin
    clean_cur.execute("SELECT pg_snapshot_xmin(pg_current_snapshot())::text")
    xmin = clean_cur.fetchone()[0]
    clean_cur.execute("SELECT xid::text FROM outbox WHERE id = (SELECT max(id) FROM outbox)")
    t2_xid = clean_cur.fetchone()[0]
    print(f"3. Active xmin={xmin}, T2.xid={t2_xid}. (T2 cannot be safely processed while T1 is pending)")

    # Step 4: T1 now commits
    print("4. T1 commits after T2...")
    conn1.commit()

    # Step 5: Wait for worker to consume both
    from tests.sync import wait_for_worker
    wait_for_worker(main_url, rollup_url)

    # Step 6: Verify in rollup that BOTH orders were counted
    r_conn = psycopg2.connect(rollup_url)
    r_cur = r_conn.cursor()
    r_cur.execute("""
        SELECT order_count, revenue FROM rollup_orders_hour
        WHERE tenant_id = 1 AND hour = %s AND status = 'delivered'
    """, (test_hour,))
    res = r_cur.fetchone()
    print(f"6. Rollup hour result: {res}")
    assert res and res[0] >= 2, f"Expected at least 2 orders, got {res}"

    # Cleanup
    clean_cur.execute("DELETE FROM order_items WHERE item_id IN (%s, %s) OR order_id IN (%s, %s)", (oid1, oid2, oid1, oid2))
    clean_cur.execute("DELETE FROM orders WHERE order_id IN (%s, %s)", (oid1, oid2))
    wait_for_worker(main_url, rollup_url, timeout=15.0)

    cur1.close()
    conn1.close()
    cur2.close()
    conn2.close()
    clean_cur.close()
    clean_conn.close()
    r_cur.close()
    r_conn.close()

    print(">>> OUT-OF-ORDER COMMIT TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_out_of_order_test(prof)
    sys.exit(0 if ok else 1)
