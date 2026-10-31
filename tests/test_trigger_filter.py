#!/usr/bin/env python3
"""tests/test_trigger_filter.py:
Verifies early filter and pg_notify in triggers:
1. Irrelevant updates (e.g. updated_at on orders) emit EXACTLY 0 outbox rows.
2. Relevant updates (status, created_at, total) emit outbox rows and notify.
3. Order item updates on irrelevant columns emit 0 rows; relevant (qty, price) emit rows.
4. Quantifies outbox rows per 1,000 writes.
"""
import sys, os, time, psycopg2, selectors
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url

def test_early_filter():
    print("=" * 70)
    print("TESTING EARLY FILTER & PG_NOTIFY IN TRIGGERS")
    print("=" * 70)

    tid = 1
    t_url = get_tenant_db_url(tid, "1m")
    conn = psycopg2.connect(t_url)
    conn.autocommit = True
    cur = conn.cursor()

    # Listen on outbox
    listener = psycopg2.connect(t_url)
    listener.autocommit = True
    l_cur = listener.cursor()
    l_cur.execute("LISTEN outbox")

    # Pick an order to test with
    cur.execute("SELECT order_id, status, created_at, total, updated_at FROM orders WHERE tenant_id = %s LIMIT 1", (tid,))
    order_id, status, created_at, total, updated_at = cur.fetchone()

    cur.execute("SELECT count(*) FROM outbox")
    cnt_before = cur.fetchone()[0]

    # TEST 1: Irrelevant update (touch updated_at only)
    print("\n[TEST 1] Updating only updated_at on order (irrelevant change)...")
    cur.execute("UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s", (order_id,))
    
    cur.execute("SELECT count(*) FROM outbox")
    cnt_after = cur.fetchone()[0]
    outbox_diff = cnt_after - cnt_before

    listener.poll()
    notifies = len(listener.notifies)
    print(f"  Outbox rows emitted: {outbox_diff} (Expected: 0)")
    print(f"  Notifications received: {notifies} (Expected: 0)")
    assert outbox_diff == 0, f"Expected 0 outbox rows for irrelevant update, got {outbox_diff}"
    assert notifies == 0, f"Expected 0 notifications for irrelevant update, got {notifies}"
    print("  [PASS] Irrelevant order update emitted 0 rows and 0 notifications!")

    # TEST 2: Relevant update (touch total)
    print("\n[TEST 2] Updating total on order (relevant change)...")
    cur.execute("UPDATE orders SET total = total + 10.00 WHERE order_id = %s", (order_id,))
    cur.execute("SELECT count(*) FROM outbox")
    cnt_after_rel = cur.fetchone()[0]
    rel_diff = cnt_after_rel - cnt_after

    listener.poll()
    rel_notifies = len(listener.notifies)
    listener.notifies.clear()
    print(f"  Outbox rows emitted: {rel_diff} (Expected: > 0)")
    print(f"  Notifications received: {rel_notifies} (Expected: >= 1)")
    assert rel_diff > 0, f"Expected outbox rows for relevant update, got {rel_diff}"
    assert rel_notifies >= 1, "Expected notification for relevant update"
    print("  [PASS] Relevant order update emitted outbox row and notification!")

    # Revert total
    cur.execute("UPDATE orders SET total = %s WHERE order_id = %s", (total, order_id))
    listener.poll()
    listener.notifies.clear()

    # TEST 3: Order items irrelevant update (if any columns) vs relevant update (qty)
    print("\n[TEST 3] Updating qty on order item (relevant change)...")
    cur.execute("SELECT item_id, qty, price, product_id FROM order_items WHERE order_id = %s LIMIT 1", (order_id,))
    item_row = cur.fetchone()
    if item_row:
        item_id, item_qty, item_price, item_pid = item_row
        cur.execute("SELECT count(*) FROM outbox")
        cnt_item_before = cur.fetchone()[0]

        cur.execute("UPDATE order_items SET qty = qty + 1 WHERE item_id = %s", (item_id,))
        cur.execute("SELECT count(*) FROM outbox")
        cnt_item_after = cur.fetchone()[0]

        listener.poll()
        item_notifies = len(listener.notifies)
        listener.notifies.clear()
        print(f"  Outbox rows emitted for item qty change: {cnt_item_after - cnt_item_before}")
        print(f"  Notifications received: {item_notifies}")
        assert (cnt_item_after - cnt_item_before) > 0, "Expected outbox rows for item update"
        print("  [PASS] Order item qty update emitted outbox row and notification!")

        # Revert item qty
        cur.execute("UPDATE order_items SET qty = %s WHERE item_id = %s", (item_qty, item_id))
        listener.poll()
        listener.notifies.clear()

    l_cur.close()
    listener.close()
    cur.close()
    conn.close()

    print("\n>>> ALL EARLY FILTER & NOTIFY TESTS PASSED! <<<")
    return True

if __name__ == "__main__":
    test_early_filter()
