#!/usr/bin/env python3
"""tests/test_concurrent_multi_tenant.py:
Tests concurrent multi-tenant mutations across 25 tenant databases:
- Concurrently mutates 25 separate tenant databases (inserts, cancels, uncancels, timestamp moves).
- Uses valid per-tenant product IDs and high disjoint order IDs.
- Waits for worker catch-up across all touched tenants.
- Verifies 100% exact ground-truth parity for every touched tenant against its own database.
- Cleans up all test mutations after verification.
"""
import sys, os, time, datetime, psycopg2
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def mutate_single_tenant(tid: int, profile: str = "1m"):
    url = get_tenant_db_url(tid, profile)
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()

    # Disjoint ID space above dataset range
    oid = 85_000_000 + tid
    iid = 85_000_000 + tid
    ts1 = datetime.datetime(2026, 10, 25, 12, 0, 0)
    ts2 = datetime.datetime(2026, 10, 26, 15, 0, 0)

    # 1. Clean previous run if any
    cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
    cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))

    # 2. Get valid product_id for this tenant
    cur.execute("SELECT product_id, price FROM products WHERE tenant_id = %s LIMIT 1", (tid,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT product_id, price FROM products LIMIT 1")
        row = cur.fetchone()
    pid, pr = row[0], float(row[1])
    qty = 2
    total = pr * qty

    # 3. Insert order
    cur.execute("""
        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
        VALUES (%s, %s, 1, 'delivered', %s, %s, %s)
    """, (oid, tid, total, ts1, ts1))
    cur.execute("""
        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (iid, oid, tid, pid, qty, pr))

    # 4. Cancel flip
    cur.execute("UPDATE orders SET status = 'canceled', updated_at = now() WHERE order_id = %s", (oid,))

    # 5. Uncancel flip
    cur.execute("UPDATE orders SET status = 'delivered', updated_at = now() WHERE order_id = %s", (oid,))

    # 6. Timestamp move across day boundary
    cur.execute("UPDATE orders SET created_at = %s, updated_at = now() WHERE order_id = %s", (ts2, oid))

    cur.close()
    conn.close()
    return tid, oid

def run_concurrent_multi_tenant_test(profile: str = "1m", tenant_count: int = 25) -> bool:
    print("=" * 70)
    print(f"RUNNING CONCURRENT MULTI-TENANT MUTATIONS ({tenant_count} TENANT DBS)")
    print("=" * 70)

    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    excluded = tuple(cfg["business_rules"]["excluded_statuses"])
    target_tids = list(range(1, tenant_count + 1))

    # Execute mutations across 25 tenants concurrently
    print(f"Executing concurrent mutations on {len(target_tids)} tenant DBs in parallel...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda t: mutate_single_tenant(t, profile), target_tids))
    print(f"Mutations finished on {len(results)} tenant DBs in {time.time() - t0:.2f}s")

    # Wait for worker catch-up
    print("Waiting for rollup worker to process all outbox deltas...")
    for tid, _ in results:
        wait_for_worker(profile, tenant_id=tid, timeout=15.0)

    # Verify ground-truth parity for EVERY touched tenant
    print("Verifying ground-truth parity for all 25 touched tenants against their OWN DBs...")
    r_conn = psycopg2.connect(rollup_url)
    r_cur = r_conn.cursor()

    mismatches = 0
    for tid, oid in results:
        t_url = get_tenant_db_url(tid, profile)
        tc = psycopg2.connect(t_url)
        tcur = tc.cursor()

        # Check raw orders vs rollup_orders_hour
        tcur.execute("""
            SELECT count(*), coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
            FROM orders WHERE tenant_id = %s
        """, (excluded, tid))
        sql_cnt, sql_rev = tcur.fetchone()

        r_cur.execute("""
            SELECT coalesce(sum(order_count), 0), coalesce(sum(revenue), 0)
            FROM rollup_orders_hour WHERE tenant_id = %s
        """, (tid,))
        r_cnt, r_rev = r_cur.fetchone()

        # Check raw items vs rollup_product_day
        tcur.execute("""
            SELECT coalesce(sum(oi.qty), 0), coalesce(sum(oi.qty * oi.price), 0)
            FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
            WHERE o.status NOT IN %s AND o.tenant_id = %s
        """, (excluded, tid))
        sql_units, sql_sales = tcur.fetchone()

        r_cur.execute("""
            SELECT coalesce(sum(units), 0), coalesce(sum(sales), 0)
            FROM rollup_product_day WHERE tenant_id = %s
        """, (tid,))
        r_units, r_sales = r_cur.fetchone()

        cnt_match = (sql_cnt == r_cnt)
        rev_match = (abs(float(sql_rev) - float(r_rev)) < 0.01)
        units_match = (sql_units == r_units)
        sales_match = (abs(float(sql_sales) - float(r_sales)) < 0.01)

        if not (cnt_match and rev_match and units_match and sales_match):
            print(f"[FAIL] Parity mismatch on Tenant {tid}: SQL=({sql_cnt},{sql_rev},{sql_units},{sql_sales}) vs Rollup=({r_cnt},{r_rev},{r_units},{r_sales})")
            mismatches += 1

        # Clean up test order
        tcur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
        tcur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
        tcur.close()
        tc.close()

    r_cur.close()
    r_conn.close()

    # Wait for deletion catch-up
    for tid in target_tids:
        wait_for_worker(profile, tenant_id=tid, timeout=10.0)

    print(f"Concurrent Parity Result: {len(target_tids) - mismatches}/{len(target_tids)} tenants passed (mismatches={mismatches})")
    assert mismatches == 0, f"Found {mismatches} mismatches after concurrent multi-tenant mutations"
    print(">>> CONCURRENT MULTI-TENANT TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_concurrent_multi_tenant_test(profile=prof)
    sys.exit(0 if ok else 1)
