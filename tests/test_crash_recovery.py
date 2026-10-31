#!/usr/bin/env python3
"""tests/test_crash_recovery.py:
Tests worker crash mid-batch and recovery per-tenant:
Verifies across at least 3 tenants (including largest Tenant 1, plus 42 and 99):
1. Spawns worker.
2. Injects rapid mutations into all 3 tenant databases.
3. Abruptly terminates (kills) the worker process while processing.
4. Injects additional mutations while worker is dead.
5. Restarts the worker.
6. Verifies all 3 tenants catch up to match plain SQL ground truth with 0 mismatches.
"""
import sys, os, subprocess, time, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_crash_recovery_test(profile: str = "1m") -> bool:
    print(f"=== Running Per-Tenant Crash Recovery Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    test_tenants = [1, 42, 99]

    # 1. Start worker subprocess
    print("1. Starting worker process...")
    p = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile])
    time.sleep(1.5)

    base_id = 999_888_000
    t_conns = {}
    t_curs = {}
    p2 = None

    try:
        for tid in test_tenants:
            url = get_tenant_db_url(tid, profile)
            c = psycopg2.connect(url)
            c.autocommit = True
            cur = c.cursor()
            t_conns[tid] = c
            t_curs[tid] = cur
            # Pre-cleanup full range
            cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id < %s", (base_id, base_id + 200_000))
            cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id < %s", (base_id, base_id + 200_000))

        # 2. Inject active bulk orders across all 3 tenants
        print("2. Generating active bulk orders on all 3 tenant DBs...")
        for tid in test_tenants:
            cur = t_curs[tid]
            cur.execute("SELECT product_id FROM products WHERE tenant_id = %s LIMIT 1", (tid,))
            row = cur.fetchone()
            pid = row[0] if row else 1

            for i in range(25):
                oid = base_id + (tid * 1000) + i
                cur.execute("""
                    INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                    VALUES (%s, %s, 1, 'delivered', 50.00, '2026-10-28 15:00:00', now())
                """, (oid, tid))
                cur.execute("""
                    INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                    VALUES (%s, %s, %s, %s, 1, 50.00)
                """, (oid, oid, tid, pid))

        # 3. Abruptly KILL worker process mid-flight
        print("3. Killing worker process abruptly (SIGKILL)...")
        p.kill()
        p.wait()
        print("  Worker killed.")

        # 4. Inject further orders while worker is offline
        print("4. Injecting further orders while worker is offline...")
        for tid in test_tenants:
            cur = t_curs[tid]
            cur.execute("SELECT product_id FROM products WHERE tenant_id = %s LIMIT 1", (tid,))
            row = cur.fetchone()
            pid = row[0] if row else 1

            for i in range(25, 50):
                oid = base_id + (tid * 1000) + i
                cur.execute("""
                    INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                    VALUES (%s, %s, 1, 'delivered', 50.00, '2026-10-28 15:00:00', now())
                """, (oid, tid))
                cur.execute("""
                    INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                    VALUES (%s, %s, %s, %s, 1, 50.00)
                """, (oid, oid, tid, pid))

        # 5. Restart worker
        print("5. Restarting worker to recover...")
        p2 = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile])

        for tid in test_tenants:
            wait_for_worker(profile, tenant_id=tid, timeout=15.0)

        # 6. Verify WHOLE-TENANT totals (orders, revenue, units, sales) vs rollup for all 3 tenants
        r_conn = psycopg2.connect(rollup_url)
        r_cur = r_conn.cursor()
        excluded = tuple(cfg["business_rules"]["excluded_statuses"])

        print("6. Verifying WHOLE-TENANT ground-truth totals for all 3 tenants:")
        for tid in test_tenants:
            cur = t_curs[tid]

            # Whole-tenant orders & revenue
            cur.execute("""
                SELECT count(*), coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
                FROM orders WHERE tenant_id = %s
            """, (excluded, tid))
            sql_orders, sql_rev = cur.fetchone()

            r_cur.execute("""
                SELECT coalesce(sum(order_count), 0), coalesce(sum(revenue), 0) 
                FROM rollup_orders_hour WHERE tenant_id = %s
            """, (tid,))
            r_orders, r_rev = r_cur.fetchone()

            # Whole-tenant units & sales
            cur.execute("""
                SELECT coalesce(sum(oi.qty), 0), coalesce(sum(oi.qty * oi.price), 0)
                FROM order_items oi 
                JOIN orders o ON o.order_id = oi.order_id
                WHERE o.tenant_id = %s AND o.status NOT IN %s
            """, (tid, excluded))
            sql_units, sql_sales = cur.fetchone()

            r_cur.execute("""
                SELECT coalesce(sum(units), 0), coalesce(sum(sales), 0) 
                FROM rollup_product_day WHERE tenant_id = %s
            """, (tid,))
            r_units, r_sales = r_cur.fetchone()

            print(f"  Tenant {tid:2d} Whole-DB Equality:")
            print(f"    Orders : SQL={sql_orders:,} vs Rollup={r_orders:,}")
            print(f"    Revenue: SQL=${float(sql_rev):,.2f} vs Rollup=${float(r_rev):,.2f}")
            print(f"    Units  : SQL={sql_units:,} vs Rollup={r_units:,}")
            print(f"    Sales  : SQL=${float(sql_sales):,.2f} vs Rollup=${float(r_sales):,.2f}")

            assert sql_orders == r_orders, f"Tenant {tid} whole-tenant orders mismatch: {sql_orders} != {r_orders}"
            assert abs(float(sql_rev) - float(r_rev)) < 0.01, f"Tenant {tid} whole-tenant revenue mismatch: {sql_rev} != {r_rev}"
            assert sql_units == r_units, f"Tenant {tid} whole-tenant units mismatch: {sql_units} != {r_units}"
            assert abs(float(sql_sales) - float(r_sales)) < 0.01, f"Tenant {tid} whole-tenant sales mismatch: {sql_sales} != {r_sales}"

        r_cur.close()
        r_conn.close()

    finally:
        # Guaranteed cleanup
        for tid, cur in t_curs.items():
            try:
                cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id < %s", (base_id, base_id + 200_000))
                cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id < %s", (base_id, base_id + 200_000))
            except Exception:
                pass

        if p2 and p2.poll() is None:
            for tid in test_tenants:
                try: wait_for_worker(profile, tenant_id=tid, timeout=10.0)
                except Exception: pass
            p2.terminate()
            p2.wait()
        elif p and p.poll() is None:
            p.terminate()
            p.wait()

        for cur in t_curs.values():
            try: cur.close()
            except Exception: pass
        for c in t_conns.values():
            try: c.close()
            except Exception: pass

    print("\n>>> PER-TENANT CRASH RECOVERY TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_crash_recovery_test(prof)
    sys.exit(0 if ok else 1)
