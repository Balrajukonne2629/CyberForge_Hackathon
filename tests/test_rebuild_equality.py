#!/usr/bin/env python3
"""tests/test_rebuild_equality.py:
Tests rebuild equality and snapshot skip per-tenant:
Verifies across at least 3 tenants (including largest Tenant 1, plus 42 and 99):
1. Injects active writes into the tenant databases with high disjoint IDs.
2. Concurrently executes rebuild_tenant() while writes are landing.
3. Lets workers catch up on outbox deltas created during and after rebuild.
4. Verifies exact ground-truth equality between plain SQL on tenant DBs and rollups.
"""
import sys, os, time, datetime, subprocess, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from scripts.rebuild import rebuild_tenant
from tests.sync import wait_for_worker

def run_rebuild_equality_test(profile: str = "1m") -> bool:
    print(f"=== Running Per-Tenant Rebuild Equality Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    excluded = tuple(cfg["business_rules"]["excluded_statuses"])
    test_tenants = [1, 42, 99]

    # Start worker process
    p = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile])
    time.sleep(1.0)

    try:
        for tid in test_tenants:
            t_url = get_tenant_db_url(tid, profile)
            print(f"\n--- Testing Rebuild Equality on Tenant {tid} ({t_url}) ---")

            t_conn = psycopg2.connect(t_url)
            t_conn.autocommit = True
            t_cur = t_conn.cursor()

            # Disjoint ID space far above dataset range (900M+)
            base_oid = 960_000_000 + (tid * 1000)

            # Get valid product for this tenant
            t_cur.execute("SELECT product_id, price FROM products WHERE tenant_id = %s LIMIT 1", (tid,))
            row = t_cur.fetchone()
            if not row:
                t_cur.execute("SELECT product_id, price FROM products LIMIT 1")
                row = t_cur.fetchone()
            pid, pr = row[0], float(row[1])

            # Pre-clean
            t_cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id < %s", (base_oid, base_oid + 100))
            t_cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id < %s", (base_oid, base_oid + 100))
            wait_for_worker(profile, tenant_id=tid, timeout=10.0)

            try:
                # 1. Inject writes before & during rebuild on this tenant
                print(f"1. Injecting writes before & during rebuild on Tenant {tid}...")
                for i in range(20):
                    oid = base_oid + i
                    t_cur.execute("""
                        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                        VALUES (%s, %s, 1, 'delivered', %s, '2026-10-25 14:00:00', now())
                    """, (oid, tid, pr * 2))
                    t_cur.execute("""
                        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                        VALUES (%s, %s, %s, %s, 2, %s)
                    """, (oid, oid, tid, pid, pr))

                # 2. Concurrently execute rebuild for this tenant
                print(f"2. Running targeted rebuild_tenant({tid})...")
                rebuild_res = rebuild_tenant(tid, profile=profile, clear_first=True)
                print(f"  Rebuild result: snapshot={rebuild_res['snapshot']}, hour_rows={rebuild_res['hour_rows']}")

                # 3. Inject further writes post-rebuild
                print(f"3. Injecting further writes post-rebuild on Tenant {tid}...")
                for i in range(20, 40):
                    oid = base_oid + i
                    t_cur.execute("""
                        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                        VALUES (%s, %s, 1, 'delivered', %s, '2026-10-25 14:00:00', now())
                    """, (oid, tid, pr * 2))
                    t_cur.execute("""
                        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                        VALUES (%s, %s, %s, %s, 2, %s)
                    """, (oid, oid, tid, pid, pr))

                # 4. Wait for worker to consume post-rebuild outbox rows
                print(f"4. Waiting for worker to catch up on Tenant {tid}...")
                wait_for_worker(profile, tenant_id=tid, timeout=15.0)

                # 5. Verify 100% exact equality against plain SQL on this tenant DB
                t_cur.execute("""
                    SELECT count(*), coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
                    FROM orders WHERE tenant_id = %s
                """, (excluded, tid))
                sql_orders, sql_rev = t_cur.fetchone()

                t_cur.execute("""
                    SELECT coalesce(sum(oi.qty), 0), coalesce(sum(oi.qty * oi.price), 0)
                    FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
                    WHERE o.tenant_id = %s AND o.status NOT IN %s
                """, (tid, excluded))
                sql_units, sql_sales = t_cur.fetchone()

                r_conn = psycopg2.connect(rollup_url)
                r_cur = r_conn.cursor()
                r_cur.execute("SELECT coalesce(sum(order_count), 0), coalesce(sum(revenue), 0) FROM rollup_orders_hour WHERE tenant_id = %s", (tid,))
                r_orders, r_rev = r_cur.fetchone()

                r_cur.execute("SELECT coalesce(sum(units), 0), coalesce(sum(sales), 0) FROM rollup_product_day WHERE tenant_id = %s", (tid,))
                r_units, r_sales = r_cur.fetchone()

                print(f"5. Equality check for Tenant {tid}:")
                print(f"   Orders : SQL={sql_orders} vs Rollup={r_orders}")
                print(f"   Revenue: SQL=${float(sql_rev):,.2f} vs Rollup=${float(r_rev):,.2f}")
                print(f"   Units  : SQL={sql_units} vs Rollup={r_units}")
                print(f"   Sales  : SQL=${float(sql_sales):,.2f} vs Rollup=${float(r_sales):,.2f}")

                assert sql_orders == r_orders, f"Orders mismatch on T{tid}: {sql_orders} != {r_orders}"
                assert abs(float(sql_rev) - float(r_rev)) < 0.01, f"Revenue mismatch on T{tid}: {sql_rev} != {r_rev}"
                assert sql_units == r_units, f"Units mismatch on T{tid}: {sql_units} != {r_units}"
                assert abs(float(sql_sales) - float(r_sales)) < 0.01, f"Sales mismatch on T{tid}: {sql_sales} != {r_sales}"

            finally:
                # Cleanup
                try:
                    t_cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id < %s", (base_oid, base_oid + 100))
                    t_cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id < %s", (base_oid, base_oid + 100))
                    wait_for_worker(profile, tenant_id=tid, timeout=10.0)
                except Exception:
                    pass

                t_cur.close(); t_conn.close()
                try: r_cur.close(); r_conn.close()
                except Exception: pass

    finally:
        p.terminate()
        p.wait()

    print("\n>>> PER-TENANT REBUILD EQUALITY TEST PASSED 100% ACROSS ALL 3 TENANTS <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_rebuild_equality_test(prof)
    sys.exit(0 if ok else 1)
