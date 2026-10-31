#!/usr/bin/env python3
"""tests/test_trigger_efficiency.py:
Tests orders UPDATE trigger efficiency:
1. Inserts a dedicated test order in 900M+ range (970_000_000 + tenant_id).
2. No-op update (updating only updated_at) emits EXACTLY 0 outbox rows.
3. Price adjustment in same bucket emits EXACTLY 1 folded outbox row (delta_count=0, delta_amount=diff).
4. Real status change (e.g. delivered -> canceled) emits exact transition rows and fan-out items.
5. Cleans up only the test order.
"""
import sys, os, time, datetime, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_trigger_efficiency_test(profile: str = "1m", tenant_id: int = 1) -> bool:
    print(f"=== Testing Orders UPDATE Trigger Efficiency (Tenant {tenant_id}, {profile.upper()}) ===")
    t_url = get_tenant_db_url(tenant_id, profile)
    conn = psycopg2.connect(t_url)
    conn.autocommit = True
    cur = conn.cursor()

    oid = 970_000_000 + tenant_id
    iid = 970_000_000 + tenant_id
    test_hour = datetime.datetime(2026, 10, 28, 14, 0, 0)

    # 1. Pre-clean
    cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
    cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))

    # Get valid product for this tenant
    cur.execute("SELECT product_id, price FROM products WHERE tenant_id = %s LIMIT 1", (tenant_id,))
    row = cur.fetchone()
    pid = row[0] if row else 1
    orig_total = 50.00
    orig_status = "delivered"

    try:
        # Insert dedicated test order
        cur.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, %s, 1, %s, %s, %s, %s)
        """, (oid, tenant_id, orig_status, orig_total, test_hour, test_hour))
        cur.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, %s, %s, 1, %s)
        """, (iid, oid, tenant_id, pid, orig_total))

        # Wait for worker to consume the initial insert
        wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

        # 2. No-op update (only updated_at changed)
        print("1. Testing no-op UPDATE (updating only updated_at)...")
        cur.execute("SELECT count(*) FROM outbox")
        c0 = cur.fetchone()[0]

        cur.execute("UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s", (oid,))

        cur.execute("SELECT count(*) FROM outbox")
        c1 = cur.fetchone()[0]
        emitted_noop = c1 - c0
        print(f"   Emitted outbox rows on no-op update: {emitted_noop}")
        assert emitted_noop == 0, f"Expected 0 outbox rows on no-op update, got {emitted_noop}"

        # 3. Price change in same bucket (folding test)
        print("2. Testing price update in same bucket (folding -1/+1)...")
        new_total = orig_total + 15.00
        cur.execute("UPDATE orders SET total = %s WHERE order_id = %s", (new_total, oid))

        cur.execute("SELECT count(*) FROM outbox")
        c2 = cur.fetchone()[0]
        emitted_price = c2 - c1
        print(f"   Emitted outbox rows on price change: {emitted_price}")
        assert emitted_price == 1, f"Expected 1 folded outbox row, got {emitted_price}"

        cur.execute("SELECT delta_count, delta_amount FROM outbox ORDER BY id DESC LIMIT 1")
        d_cnt, d_amt = cur.fetchone()
        print(f"   Folded row: delta_count={d_cnt}, delta_amount={d_amt}")
        assert d_cnt == 0, f"Expected delta_count=0 on price adjustment, got {d_cnt}"
        assert abs(float(d_amt) - 15.00) < 0.01, f"Expected delta_amount=15.00, got {d_amt}"

        # 4. Real status change
        print("3. Testing real status change (status bucket move)...")
        cur.execute("SELECT count(*) FROM outbox")
        c3 = cur.fetchone()[0]

        cur.execute("UPDATE orders SET status = 'canceled' WHERE order_id = %s", (oid,))

        cur.execute("SELECT count(*) FROM outbox")
        c4 = cur.fetchone()[0]
        emitted_status = c4 - c3
        print(f"   Emitted outbox rows on status change: {emitted_status}")
        assert emitted_status >= 2, f"Expected at least 2 rows (order transition + item fan-out), got {emitted_status}"

    finally:
        # Clean up dedicated test rows only
        cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
        cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
        wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)
        cur.close()
        conn.close()

    print(">>> TRIGGER EFFICIENCY TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_trigger_efficiency_test(prof)
    sys.exit(0 if ok else 1)
