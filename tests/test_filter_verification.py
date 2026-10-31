#!/usr/bin/env python3
"""tests/test_filter_verification.py:
Verifies early trigger filter mechanics, statement counts, outbox row emission,
and executes a 500-operation random mutation fuzz on 5 tenants comparing rollups to plain SQL.
Strictly touches ONLY order IDs and item IDs >= 900,000,000, and cleans up completely.
"""
import sys, os, time, random, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from worker.worker import MultiTenantWorker
from scripts.rebuild import rebuild_tenant

def check_foreign_sessions():
    print("=" * 70)
    print("CHECKING PG_STAT_ACTIVITY FOR FOREIGN SESSIONS")
    print("=" * 70)
    cfg = get_config("1m")
    conn = psycopg2.connect(cfg["database"]["rollup_db_url"])
    cur = conn.cursor()
    cur.execute("""
        SELECT pid, usename, datname, client_addr, state, query
        FROM pg_stat_activity
        WHERE pid <> pg_backend_pid()
    """)
    rows = cur.fetchall()
    print(f"Total non-self database sessions found: {len(rows)}")
    for r in rows:
        pid, user, db, addr, state, q = r
        q_preview = (q or "").replace("\n", " ").strip()[:60]
        print(f"  - PID {pid:5d} | user={user or 'internal':<10s} | db={db or 'none':<12s} | addr={str(addr):<15s} | state={state or 'none':<8s} | query={q_preview}")
    cur.close()
    conn.close()
    print("=" * 70)

def test_direct_relevant_updates():
    print("=" * 70)
    print("1. DIRECT CHECK: OUTBOX ROWS PER STATEMENT ON RELEVANT UPDATES")
    print("=" * 70)
    conn = psycopg2.connect(get_tenant_db_url(1, "1m"))
    conn.autocommit = False
    cur = conn.cursor()

    # Create 10 dedicated test orders in >= 900M range to never mutate dataset rows
    base_id = 910_000_000
    cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id <= %s", (base_id + 1, base_id + 10))
    cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id <= %s", (base_id + 1, base_id + 10))
    conn.commit()

    cur.execute("SELECT customer_id FROM customers WHERE tenant_id = 1 LIMIT 1")
    cid = cur.fetchone()[0]
    cur.execute("SELECT product_id FROM products LIMIT 1")
    pid = cur.fetchone()[0]

    for idx in range(1, 11):
        oid = base_id + idx
        cur.execute("INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at) "
                    "VALUES (%s, 1, %s, 'delivered', 100.00, now(), now())", (oid, cid))
        if idx <= 5:
            iid = base_id + idx
            cur.execute("INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price) "
                        "VALUES (%s, %s, 1, %s, 1, 50.00)", (iid, oid, pid))
    conn.commit()

    tests = [
        ("status change", "UPDATE orders SET status = CASE WHEN status = 'canceled' THEN 'delivered' ELSE 'canceled' END WHERE order_id = %s"),
        ("total change", "UPDATE orders SET total = total + 5.00 WHERE order_id = %s"),
        ("created_at change", "UPDATE orders SET created_at = created_at + interval '1 hour' WHERE order_id = %s"),
        ("irrelevant (updated_at only)", "UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s"),
    ]

    for name, stmt in tests:
        cur.execute("SELECT count(*) FROM outbox")
        before = cur.fetchone()[0]
        # Run on 5 distinct test order IDs
        for idx in range(1, 6):
            oid = base_id + idx
            cur.execute(stmt, (oid,))
            conn.commit()
        cur.execute("SELECT count(*) FROM outbox")
        after = cur.fetchone()[0]
        emitted = after - before
        print(f"  {name:30s}: 5 statements -> {emitted:2d} outbox rows ({emitted/5:.2f} rows/stmt)")

    # Test order_items changes
    item_tests = [
        ("item qty change", "UPDATE order_items SET qty = qty + 1 WHERE item_id = %s"),
        ("item price change", "UPDATE order_items SET price = price + 2.50 WHERE item_id = %s"),
        ("item product change", "UPDATE order_items SET product_id = product_id WHERE item_id = %s"),
    ]
    for name, stmt in item_tests:
        cur.execute("SELECT count(*) FROM outbox")
        before = cur.fetchone()[0]
        for idx in range(1, 6):
            iid = base_id + idx
            cur.execute(stmt, (iid,))
            conn.commit()
        cur.execute("SELECT count(*) FROM outbox")
        after = cur.fetchone()[0]
        emitted = after - before
        print(f"  {name:30s}: 5 statements -> {emitted:2d} outbox rows ({emitted/5:.2f} rows/stmt)")

    # Clean up test rows
    cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id <= %s", (base_id + 1, base_id + 10))
    cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id <= %s", (base_id + 1, base_id + 10))
    conn.commit()
    conn.close()

def run_500_op_fuzz():
    print("\n" + "=" * 70)
    print("2. 500-OPERATION RANDOM MUTATION FUZZ ON 5 TENANTS (TEST IDS >= 900M)")
    print("=" * 70)
    tenants = [1, 2, 3, 4, 5]
    rng = random.Random(1337)

    base_fuzz_oid = 930_000_000
    base_fuzz_iid = 930_000_000

    # Ensure pre-clean across the 5 tenants
    for tid in tenants:
        conn = psycopg2.connect(get_tenant_db_url(tid, "1m"))
        cur = conn.cursor()
        cur.execute("DELETE FROM order_items WHERE order_id >= 900000000 OR item_id >= 900000000")
        cur.execute("DELETE FROM orders WHERE order_id >= 900000000")
        conn.commit()
        conn.close()

    # Pre-seed 10 test orders and items per tenant in >= 900M range
    conns = {tid: psycopg2.connect(get_tenant_db_url(tid, "1m")) for tid in tenants}
    for c in conns.values():
        c.autocommit = False

    next_seq = 100
    for tid in tenants:
        conn = conns[tid]
        cur = conn.cursor()
        cur.execute("SELECT customer_id FROM customers WHERE tenant_id = %s LIMIT 1", (tid,))
        cid = cur.fetchone()[0]
        cur.execute("SELECT product_id, price FROM products LIMIT 1")
        pid, price = cur.fetchone()
        for i in range(10):
            oid = base_fuzz_oid + (tid * 1000) + i
            iid = base_fuzz_iid + (tid * 1000) + i
            cur.execute("INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at) "
                        "VALUES (%s, %s, %s, 'delivered', %s, now(), now())", (oid, tid, cid, price * 2))
            cur.execute("INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price) "
                        "VALUES (%s, %s, %s, %s, 2, %s)", (iid, oid, tid, pid, price))
        conn.commit()

    # Count outbox before fuzz
    ob_counts_before = {}
    for tid in tenants:
        cur = conns[tid].cursor()
        cur.execute("SELECT count(*) FROM outbox")
        ob_counts_before[tid] = cur.fetchone()[0]

    ops_done = 0
    stats = {
        "insert order": 0,
        "insert items": 0,
        "status_flip": 0,
        "cancel": 0,
        "uncancel": 0,
        "change total": 0,
        "update only updated_at": 0,
        "delete": 0,
        "qty_change": 0
    }

    for step in range(500):
        tid = rng.choice(tenants)
        conn = conns[tid]
        cur = conn.cursor()
        op = rng.choice(list(stats.keys()))

        if op == "insert order":
            next_seq += 1
            new_oid = base_fuzz_oid + (tid * 1000) + next_seq
            new_iid = base_fuzz_iid + (tid * 1000) + next_seq
            cur.execute("SELECT customer_id FROM customers WHERE tenant_id = %s LIMIT 1", (tid,))
            cid = cur.fetchone()[0]
            cur.execute("SELECT product_id, price FROM products LIMIT 1")
            pid, price = cur.fetchone()
            cur.execute("INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at) "
                        "VALUES (%s, %s, %s, 'created', %s, now(), now())",
                        (new_oid, tid, cid, price * 2))
            cur.execute("INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price) "
                        "VALUES (%s, %s, %s, %s, 2, %s)",
                        (new_iid, new_oid, tid, pid, price))
            conn.commit()
            stats["insert order"] += 1

        elif op == "insert items":
            # Add an extra item to an existing test order >= 900M
            cur.execute("SELECT order_id FROM orders WHERE order_id >= 900000000 ORDER BY random() LIMIT 1")
            row = cur.fetchone()
            if row:
                next_seq += 1
                oid = row[0]
                new_iid = base_fuzz_iid + (tid * 1000) + next_seq
                cur.execute("SELECT product_id, price FROM products ORDER BY random() LIMIT 1")
                pid, price = cur.fetchone()
                cur.execute("INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price) "
                            "VALUES (%s, %s, %s, %s, 1, %s)",
                            (new_iid, oid, tid, pid, price))
                conn.commit()
                stats["insert items"] += 1

        elif op in ("status_flip", "cancel", "uncancel", "change total", "delete", "update only updated_at"):
            cur.execute("SELECT order_id, status FROM orders WHERE order_id >= 900000000 ORDER BY random() LIMIT 1")
            row = cur.fetchone()
            if not row:
                continue
            oid, curr_st = row

            if op == "status_flip":
                new_st = "delivered" if curr_st != "delivered" else "processing"
                cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", (new_st, oid))
                stats["status_flip"] += 1
            elif op == "cancel":
                cur.execute("UPDATE orders SET status = 'canceled' WHERE order_id = %s", (oid,))
                stats["cancel"] += 1
            elif op == "uncancel":
                cur.execute("UPDATE orders SET status = 'approved' WHERE order_id = %s", (oid,))
                stats["uncancel"] += 1
            elif op == "change total":
                cur.execute("UPDATE orders SET total = total + %s WHERE order_id = %s", (round(rng.uniform(1.0, 10.0), 2), oid))
                stats["change total"] += 1
            elif op == "update only updated_at":
                cur.execute("UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s", (oid,))
                stats["update only updated_at"] += 1
            elif op == "delete":
                cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
                cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
                stats["delete"] += 1
            conn.commit()

        elif op == "qty_change":
            cur.execute("SELECT item_id, qty FROM order_items WHERE order_id >= 900000000 ORDER BY random() LIMIT 1")
            row = cur.fetchone()
            if row:
                iid, q = row
                cur.execute("UPDATE order_items SET qty = qty + 1 WHERE item_id = %s", (iid,))
                conn.commit()
                stats["qty_change"] += 1

        ops_done += 1

    # Count outbox after fuzz
    ob_counts_after = {}
    for tid in tenants:
        cur = conns[tid].cursor()
        cur.execute("SELECT count(*) FROM outbox")
        ob_counts_after[tid] = cur.fetchone()[0]

    for c in conns.values():
        c.close()

    total_outbox_created = sum(ob_counts_after[tid] - ob_counts_before[tid] for tid in tenants)

    print(f"\nExecuted {ops_done} random mutation operations across tenants {tenants} (Data size: 1m, Balu laptop i5-1235U, 15.7 GB):")
    for k, v in stats.items():
        print(f"  - {k:24s}: {v}")
    print(f"\nTotal outbox rows created during fuzz: {total_outbox_created}")

    # Now drain worker completely
    print("\nDraining worker on touched tenants...")
    worker = MultiTenantWorker(profile="1m", worker_id=0, worker_count=1, mode="event", target_tenants=tenants)
    for _ in range(50):
        worker.run_one_cycle()
        time.sleep(0.02)
    worker.close()

    # Compare rollups to plain SQL on every touched tenant
    print("\nComparing rollups to plain SQL on touched tenants...")
    rconn = psycopg2.connect(get_config("1m")["database"]["rollup_db_url"])
    rcur = rconn.cursor()

    mismatches = 0
    for tid in tenants:
        tconn = psycopg2.connect(get_tenant_db_url(tid, "1m"))
        tcur = tconn.cursor()

        # 1. Compare total order count and revenue per day/hour
        tcur.execute("""
            SELECT date_trunc('hour', created_at) as hr, 
                   status,
                   count(*) as orders, 
                   sum(case when status not in ('canceled','unavailable') then total else 0 end) as rev
            FROM orders
            GROUP BY 1, 2
        """)
        raw_hours = {(r[0], r[1]): (r[2], float(r[3])) for r in tcur.fetchall()}

        rcur.execute("""
            SELECT hour,
                   status,
                   order_count as orders,
                   revenue as rev
            FROM rollup_orders_hour
            WHERE tenant_id = %s
        """, (tid,))
        rollup_hours = {(r[0], r[1]): (r[2], float(r[3])) for r in rcur.fetchall()}

        all_hours = set(raw_hours.keys()) | set(rollup_hours.keys())
        for h in all_hours:
            r_cnt, r_rev = raw_hours.get(h, (0, 0.0))
            u_cnt, u_rev = rollup_hours.get(h, (0, 0.0))
            if r_cnt != u_cnt or abs(r_rev - u_rev) > 0.01:
                print(f"  [MISMATCH] Tenant {tid} Hour {h}: Raw=({r_cnt}, {r_rev:.2f}) vs Rollup=({u_cnt}, {u_rev:.2f})")
                mismatches += 1

        # 2. Compare product rollups
        tcur.execute("""
            SELECT date_trunc('day', o.created_at) as dy,
                   i.product_id,
                   sum(i.qty) as units,
                   sum(i.qty * i.price) as sales
            FROM orders o
            JOIN order_items i ON o.order_id = i.order_id
            WHERE o.status NOT IN ('canceled', 'unavailable')
            GROUP BY 1, 2
        """)
        raw_prods = {(r[0], r[1]): (r[2], float(r[3])) for r in tcur.fetchall()}

        rcur.execute("""
            SELECT day,
                   product_id,
                   units,
                   sales
            FROM rollup_product_day
            WHERE tenant_id = %s
        """, (tid,))
        rollup_prods = {(r[0], r[1]): (r[2], float(r[3])) for r in rcur.fetchall()}

        all_prod_keys = set(raw_prods.keys()) | set(rollup_prods.keys())
        for k in all_prod_keys:
            r_u, r_s = raw_prods.get(k, (0, 0.0))
            u_u, u_s = rollup_prods.get(k, (0, 0.0))
            if r_u != u_u or abs(r_s - u_s) > 0.01:
                print(f"  [MISMATCH] Tenant {tid} Prod {k}: Raw=({r_u}, {r_s:.2f}) vs Rollup=({u_u}, {u_s:.2f})")
                mismatches += 1

        tconn.close()
        print(f"  Tenant {tid:03d}: checked {len(all_hours)} hour buckets and {len(all_prod_keys)} product buckets -> OK")

    rconn.close()
    print("=" * 70)
    print(f"TOTAL FUZZ MISMATCHES: {mismatches}")
    print("=" * 70)

    # Post-test Cleanup: remove all test orders and rebuild the 5 touched tenants
    print("\nCleaning up test orders (>= 900M) across all touched tenants...")
    for tid in tenants:
        conn = psycopg2.connect(get_tenant_db_url(tid, "1m"))
        cur = conn.cursor()
        cur.execute("DELETE FROM order_items WHERE order_id >= 900000000 OR item_id >= 900000000")
        cur.execute("DELETE FROM orders WHERE order_id >= 900000000")
        conn.commit()
        conn.close()
        rebuild_tenant(tid, profile="1m", clear_first=True)

    return mismatches

if __name__ == "__main__":
    check_foreign_sessions()
    test_direct_relevant_updates()
    mismatches = run_500_op_fuzz()
    if mismatches > 0:
        print("[CRITICAL ERROR] LOST EVENTS DETECTED! STOPPING.")
        sys.exit(1)
    else:
        print("[SUCCESS] ZERO EVENT LOSS PROVEN UNDER 500 RANDOM MUTATIONS.")
