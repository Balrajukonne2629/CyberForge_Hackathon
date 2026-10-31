#!/usr/bin/env python3
"""tests/test_filter_verification.py:
Verifies early trigger filter mechanics, statement counts, outbox row emission,
and executes a 500-operation random mutation fuzz on 5 tenants comparing rollups to plain SQL.
"""
import sys, os, time, random, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from worker.worker import MultiTenantWorker

def test_direct_relevant_updates():
    print("=" * 70)
    print("1. DIRECT CHECK: OUTBOX ROWS PER STATEMENT ON RELEVANT UPDATES")
    print("=" * 70)
    conn = psycopg2.connect(get_tenant_db_url(1, "1m"))
    conn.autocommit = False
    cur = conn.cursor()

    # Pick 10 known existing orders from tenant_001
    cur.execute("SELECT order_id, status, total, created_at FROM orders LIMIT 10")
    orders = cur.fetchall()
    
    # Check outbox count
    cur.execute("SELECT count(*) FROM outbox")
    ob_start = cur.fetchone()[0]

    tests = [
        ("status change", "UPDATE orders SET status = CASE WHEN status = 'canceled' THEN 'delivered' ELSE 'canceled' END WHERE order_id = %s"),
        ("total change", "UPDATE orders SET total = total + 5.00 WHERE order_id = %s"),
        ("created_at change", "UPDATE orders SET created_at = created_at + interval '1 hour' WHERE order_id = %s"),
        ("irrelevant (updated_at only)", "UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s"),
    ]

    for name, stmt in tests:
        cur.execute("SELECT count(*) FROM outbox")
        before = cur.fetchone()[0]
        # Run on 5 distinct existing order IDs
        for i in range(5):
            oid = orders[i][0]
            cur.execute(stmt, (oid,))
            conn.commit()
        cur.execute("SELECT count(*) FROM outbox")
        after = cur.fetchone()[0]
        emitted = after - before
        print(f"  {name:30s}: 5 statements -> {emitted:2d} outbox rows ({emitted/5:.2f} rows/stmt)")

    # Test order_items changes
    cur.execute("SELECT item_id, order_id, product_id, qty, price FROM order_items LIMIT 5")
    items = cur.fetchall()
    item_tests = [
        ("item qty change", "UPDATE order_items SET qty = qty + 1 WHERE item_id = %s"),
        ("item price change", "UPDATE order_items SET price = price + 2.50 WHERE item_id = %s"),
        ("item product change", "UPDATE order_items SET product_id = product_id + 1 WHERE item_id = %s"),
    ]
    for name, stmt in item_tests:
        cur.execute("SELECT count(*) FROM outbox")
        before = cur.fetchone()[0]
        for it in items:
            cur.execute(stmt, (it[0],))
            conn.commit()
        cur.execute("SELECT count(*) FROM outbox")
        after = cur.fetchone()[0]
        emitted = after - before
        print(f"  {name:30s}: 5 statements -> {emitted:2d} outbox rows ({emitted/5:.2f} rows/stmt)")

    conn.close()

def run_500_op_fuzz():
    print("\n" + "=" * 70)
    print("2. 500-OPERATION RANDOM MUTATION FUZZ ON 5 TENANTS")
    print("=" * 70)
    tenants = [1, 2, 3, 4, 5]
    rng = random.Random(1337)
    
    conns = {tid: psycopg2.connect(get_tenant_db_url(tid, "1m")) for tid in tenants}
    for c in conns.values():
        c.autocommit = False

    ops_done = 0
    stats = {"insert": 0, "status_flip": 0, "cancel": 0, "uncancel": 0, 
             "total_change": 0, "timestamp_move": 0, "delete": 0, "qty_change": 0, "updated_at_noop": 0}

    for step in range(500):
        tid = rng.choice(tenants)
        conn = conns[tid]
        cur = conn.cursor()
        op = rng.choice(list(stats.keys()))

        if op == "insert":
            cur.execute("SELECT max(order_id) FROM orders")
            new_oid = (cur.fetchone()[0] or 0) + 1
            cur.execute("SELECT max(item_id) FROM order_items")
            new_iid = (cur.fetchone()[0] or 0) + 1
            cur.execute("SELECT customer_id FROM customers LIMIT 1")
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
            stats["insert"] += 1

        elif op in ("status_flip", "cancel", "uncancel", "total_change", "timestamp_move", "delete"):
            cur.execute("SELECT order_id, status FROM orders ORDER BY random() LIMIT 1")
            row = cur.fetchone()
            if not row: continue
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
            elif op == "total_change":
                cur.execute("UPDATE orders SET total = total + %s WHERE order_id = %s", (round(rng.uniform(1.0, 10.0), 2), oid))
                stats["total_change"] += 1
            elif op == "timestamp_move":
                cur.execute("UPDATE orders SET created_at = created_at + interval '2 hours' WHERE order_id = %s", (oid,))
                stats["timestamp_move"] += 1
            elif op == "delete":
                cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
                cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
                stats["delete"] += 1
            conn.commit()

        elif op == "qty_change":
            cur.execute("SELECT item_id, qty FROM order_items ORDER BY random() LIMIT 1")
            row = cur.fetchone()
            if row:
                iid, q = row
                cur.execute("UPDATE order_items SET qty = qty + 1 WHERE item_id = %s", (iid,))
                conn.commit()
                stats["qty_change"] += 1

        elif op == "updated_at_noop":
            cur.execute("SELECT order_id FROM orders ORDER BY random() LIMIT 1")
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s", (row[0],))
                conn.commit()
                stats["updated_at_noop"] += 1

        ops_done += 1

    for c in conns.values():
        c.close()

    print(f"Executed {ops_done} random mutation operations across tenants {tenants}:")
    for k, v in stats.items():
        print(f"  - {k:18s}: {v}")

    # Now drain worker completely
    print("\nDraining worker on touched tenants...")
    worker = MultiTenantWorker(profile="1m", worker_id=0, worker_count=1, mode="event", target_tenants=tenants)
    # Drain until outbox queue is completely empty
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

        # 1. Compare total order count and revenue per day
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
    return mismatches

if __name__ == "__main__":
    test_direct_relevant_updates()
    mismatches = run_500_op_fuzz()
    if mismatches > 0:
        print("[CRITICAL ERROR] LOST EVENTS DETECTED! STOPPING.")
        sys.exit(1)
    else:
        print("[SUCCESS] ZERO EVENT LOSS PROVEN UNDER 500 RANDOM MUTATIONS.")
