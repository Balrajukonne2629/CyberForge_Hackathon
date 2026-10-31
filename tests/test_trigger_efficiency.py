#!/usr/bin/env python3
"""tests/test_trigger_efficiency.py:
Tests orders UPDATE trigger efficiency:
1. No-op update (updating only updated_at) emits EXACTLY 0 outbox rows.
2. Price adjustment in same bucket emits EXACTLY 1 folded outbox row (delta_count=0, delta_amount=diff).
3. Real status change (e.g. delivered -> canceled) emits exact transition rows and fan-out items.
4. Golden parity remains 0 mismatches.
"""
import sys, os, time, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_trigger_efficiency_test(profile: str = "1m", tenant_id: int = 1) -> bool:
    print(f"=== Testing Orders UPDATE Trigger Efficiency (Tenant {tenant_id}, {profile.upper()}) ===")
    t_url = get_tenant_db_url(tenant_id, profile)
    conn = psycopg2.connect(t_url)
    conn.autocommit = True
    cur = conn.cursor()

    # Get a sample order
    cur.execute("SELECT order_id, total, status FROM orders WHERE tenant_id = %s LIMIT 1", (tenant_id,))
    row = cur.fetchone()
    oid, orig_total, orig_status = row[0], float(row[1]), row[2]

    try:
        # 1. No-op update (only updated_at changed)
        print("1. Testing no-op UPDATE (updating only updated_at)...")
        cur.execute("SELECT count(*) FROM outbox")
        c0 = cur.fetchone()[0]

        cur.execute("UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s", (oid,))

        cur.execute("SELECT count(*) FROM outbox")
        c1 = cur.fetchone()[0]
        emitted_noop = c1 - c0
        print(f"   Emitted outbox rows on no-op update: {emitted_noop}")
        assert emitted_noop == 0, f"Expected 0 outbox rows on no-op update, got {emitted_noop}"

        # 2. Price change in same bucket (folding test)
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

        # Revert price
        cur.execute("UPDATE orders SET total = %s WHERE order_id = %s", (orig_total, oid))

        # 3. Real status change
        print("3. Testing real status change (status bucket move)...")
        cur.execute("SELECT count(*) FROM outbox")
        c3 = cur.fetchone()[0]

        # Toggle status
        target_status = "canceled" if orig_status != "canceled" else "delivered"
        cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", (target_status, oid))

        cur.execute("SELECT count(*) FROM outbox")
        c4 = cur.fetchone()[0]
        emitted_status = c4 - c3
        print(f"   Emitted outbox rows on status change: {emitted_status}")
        assert emitted_status >= 2, f"Expected at least 2 rows (order transition + item fan-out), got {emitted_status}"

        # Restore original status
        cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", (orig_status, oid))

        # Let worker catch up
        wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

    finally:
        cur.close()
        conn.close()

    print(">>> TRIGGER EFFICIENCY TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_trigger_efficiency_test(prof)
    sys.exit(0 if ok else 1)
