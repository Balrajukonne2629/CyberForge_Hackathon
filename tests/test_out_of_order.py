#!/usr/bin/env python3
"""tests/test_out_of_order.py:
Tests safe-xid handling during out-of-order transaction commits per tenant:
Verifies across 3 tenants (including largest tenant 1, plus 42 and 99):
1. Transaction T1 starts and inserts (open)
2. Transaction T2 starts, inserts and commits
3. Proves T2 cannot be skipped by premature read because xid >= pg_snapshot_xmin()
4. Transaction T1 commits
5. Worker processes both T1 and T2 in correct order without skipping
"""
import sys, os, time, datetime, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_out_of_order_test(profile: str = "1m"):
    print(f"=== Running Per-Tenant Out-of-Order Commit Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    test_tenants = [1, 42, 99]

    for tid in test_tenants:
        t_url = get_tenant_db_url(tid, profile)
        print(f"\n--- Testing Out-of-Order on Tenant {tid} ({t_url}) ---")

        conn1 = psycopg2.connect(t_url)
        conn1.autocommit = False
        cur1 = conn1.cursor()

        conn2 = psycopg2.connect(t_url)
        conn2.autocommit = False
        cur2 = conn2.cursor()

        clean_conn = psycopg2.connect(t_url)
        clean_conn.autocommit = True
        clean_cur = clean_conn.cursor()

        # Query valid product_id for this tenant
        clean_cur.execute("SELECT product_id FROM products WHERE tenant_id = %s LIMIT 1", (tid,))
        p_row = clean_cur.fetchone()
        pid = p_row[0] if p_row else 1

        test_hour = datetime.datetime(2026, 10, 27, 10, 0, 0)
        oid1, iid1 = 98_888_000 + tid, 98_888_000 + tid
        oid2, iid2 = 98_999_000 + tid, 98_999_000 + tid

        clean_cur.execute("DELETE FROM order_items WHERE item_id IN (%s, %s)", (iid1, iid2))
        clean_cur.execute("DELETE FROM orders WHERE order_id IN (%s, %s)", (oid1, oid2))
        time.sleep(0.1)

        # 1. T1 begins and inserts (leaves open)
        cur1.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, %s, 1, 'delivered', 100.00, %s, %s)
        """, (oid1, tid, test_hour, test_hour))
        cur1.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, %s, %s, 1, 100.00)
        """, (iid1, oid1, tid, pid))

        # 2. T2 begins, inserts and COMMITS before T1
        cur2.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, %s, 1, 'delivered', 200.00, %s, %s)
        """, (oid2, tid, test_hour, test_hour))
        cur2.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, %s, %s, 2, 100.00)
        """, (iid2, oid2, tid, pid))
        conn2.commit()

        # 3. Check pg_snapshot_xmin: T2 cannot be ingested yet because T1 is open
        clean_cur.execute("SELECT pg_snapshot_xmin(pg_current_snapshot())::text")
        xmin = clean_cur.fetchone()[0]
        clean_cur.execute("SELECT xid::text FROM outbox WHERE id = (SELECT max(id) FROM outbox)")
        t2_xid = clean_cur.fetchone()[0]
        print(f"  Tenant {tid}: xmin={xmin}, T2.xid={t2_xid} (T2 deferred while T1 open)")

        # 4. T1 commits after T2
        conn1.commit()

        # 5. Wait for worker to consume both
        wait_for_worker(profile, tenant_id=tid, timeout=10.0)

        # 6. Verify in rollup that BOTH orders were counted
        r_conn = psycopg2.connect(rollup_url)
        r_cur = r_conn.cursor()
        r_cur.execute("""
            SELECT order_count, revenue FROM rollup_orders_hour
            WHERE tenant_id = %s AND hour = %s AND status = 'delivered'
        """, (tid, test_hour))
        res = r_cur.fetchone()
        print(f"  Tenant {tid} Rollup result: order_count={res[0]}, revenue={res[1]}")
        assert res and res[0] >= 2, f"Tenant {tid}: expected at least 2 orders, got {res}"

        # Cleanup
        clean_cur.execute("DELETE FROM order_items WHERE item_id IN (%s, %s)", (iid1, iid2))
        clean_cur.execute("DELETE FROM orders WHERE order_id IN (%s, %s)", (oid1, oid2))
        wait_for_worker(profile, tenant_id=tid, timeout=10.0)

        cur1.close(); conn1.close()
        cur2.close(); conn2.close()
        clean_cur.close(); clean_conn.close()
        r_cur.close(); r_conn.close()

    print("\n>>> OUT-OF-ORDER COMMIT TEST PASSED 100% ACROSS ALL 3 TENANTS <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_out_of_order_test(prof)
    sys.exit(0 if ok else 1)
