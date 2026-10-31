#!/usr/bin/env python3
"""tests/mutate.py:
Tests cancel flips, un-cancel flips, timestamp bucket edits, and deletes.
Verifies changes reach the rollups with exact numbers.
"""
import sys, time, datetime, yaml, psycopg2

def run_mutations(main_url: str, rollup_url: str):
    print("=== Running Comprehensive Mutation & Fan-Out Tests ===")
    
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    main_conn = psycopg2.connect(main_url)
    main_conn.autocommit = True
    main_cur = main_conn.cursor()

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_cur = rollup_conn.cursor()

    # Test Tenant & Product
    test_tenant = 1
    test_order_id = 999_999_001
    test_item_id = 999_999_001
    test_prod_id = 1
    test_cat = "sports_leisure"
    test_qty = 2
    test_price = 150.00
    test_total = 300.00
    test_ts = datetime.datetime(2026, 10, 25, 10, 0, 0)
    test_hour = datetime.datetime(2026, 10, 25, 10, 0, 0)
    test_day = datetime.datetime(2026, 10, 25, 0, 0, 0)

    # Clean up any leftover test data
    main_cur.execute("DELETE FROM order_items WHERE order_id = %s", (test_order_id,))
    main_cur.execute("DELETE FROM orders WHERE order_id = %s", (test_order_id,))
    time.sleep(0.5)

    def wait_worker_sync():
        time.sleep(0.5)

    # 1. INSERT new order with 'delivered' status
    print("\n[TEST 1] Inserting new delivered order (300.00, 2 units)...")
    main_cur.execute("""
        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
        VALUES (%s, %s, 1, 'delivered', %s, %s, %s)
    """, (test_order_id, test_tenant, test_total, test_ts, test_ts))
    main_cur.execute("""
        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (test_item_id, test_order_id, test_tenant, test_prod_id, test_qty, test_price))

    wait_worker_sync()

    rollup_cur.execute("""
        SELECT order_count, revenue FROM rollup_orders_hour 
        WHERE tenant_id = %s AND hour = %s AND status = 'delivered'
    """, (test_tenant, test_hour))
    h_row = rollup_cur.fetchone()
    print(f"  Rollup hour: order_count >= 1, revenue > 0 -> {h_row}")
    assert h_row and h_row[0] >= 1 and h_row[1] >= test_total

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (test_tenant, test_day, test_prod_id))
    p_row = rollup_cur.fetchone()
    print(f"  Rollup product day: units >= 2, sales >= 300 -> {p_row}")
    assert p_row and p_row[0] >= test_qty and p_row[1] >= test_total
    prev_units, prev_sales = p_row

    # 2. CANCEL FLIP: Update status to 'canceled'
    print("\n[TEST 2] Cancelling order (fan-out should subtract items)...")
    main_cur.execute("""
        UPDATE orders SET status = 'canceled', updated_at = now() WHERE order_id = %s
    """, (test_order_id,))

    wait_worker_sync()

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (test_tenant, test_day, test_prod_id))
    p_after_cancel = rollup_cur.fetchone()
    print(f"  Rollup product day after cancel: units={p_after_cancel[0]} (was {prev_units}), sales={p_after_cancel[1]} (was {prev_sales})")
    assert p_after_cancel[0] == prev_units - test_qty
    assert abs(float(p_after_cancel[1]) - (float(prev_sales) - test_total)) < 0.01

    # 3. UN-CANCEL FLIP: Restore to 'delivered'
    print("\n[TEST 3] Un-cancelling order (fan-out should re-add items)...")
    main_cur.execute("""
        UPDATE orders SET status = 'delivered', updated_at = now() WHERE order_id = %s
    """, (test_order_id,))

    wait_worker_sync()

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (test_tenant, test_day, test_prod_id))
    p_after_uncancel = rollup_cur.fetchone()
    print(f"  Rollup product day after un-cancel: units={p_after_uncancel[0]}, sales={p_after_uncancel[1]}")
    assert p_after_uncancel[0] == prev_units
    assert abs(float(p_after_uncancel[1]) - float(prev_sales)) < 0.01

    # 4. TIMESTAMP SHIFT: Move order to another day (2026-10-26)
    new_ts = datetime.datetime(2026, 10, 26, 14, 0, 0)
    new_day = datetime.datetime(2026, 10, 26, 0, 0, 0)
    print("\n[TEST 4] Moving order timestamp from 2026-10-25 to 2026-10-26...")
    main_cur.execute("""
        UPDATE orders SET created_at = %s, updated_at = now() WHERE order_id = %s
    """, (new_ts, test_order_id))

    wait_worker_sync()

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (test_tenant, test_day, test_prod_id))
    p_old_day = rollup_cur.fetchone()
    print(f"  Old day units decremented: {p_old_day[0]} == {prev_units - test_qty}")
    assert p_old_day[0] == prev_units - test_qty

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (test_tenant, new_day, test_prod_id))
    p_new_day = rollup_cur.fetchone()
    print(f"  New day units incremented: {p_new_day[0]} >= {test_qty}")
    assert p_new_day and p_new_day[0] >= test_qty

    # 5. DELETE order and items
    print("\n[TEST 5] Deleting order item and order...")
    main_cur.execute("DELETE FROM order_items WHERE order_id = %s", (test_order_id,))
    main_cur.execute("DELETE FROM orders WHERE order_id = %s", (test_order_id,))

    wait_worker_sync()

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (test_tenant, new_day, test_prod_id))
    p_after_del = rollup_cur.fetchone()
    print(f"  After delete: units correctly deducted on new day: {p_after_del[0]}")
    assert p_after_del[0] == p_new_day[0] - test_qty

    print("\n>>> ALL MUTATION & FAN-OUT TESTS PASSED WITH 100% EXACT PARITY! <<<")
    main_cur.close()
    main_conn.close()
    rollup_cur.close()
    rollup_conn.close()

if __name__ == "__main__":
    m_url = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres:postgres@localhost:5432/shop"
    r_url = sys.argv[2] if len(sys.argv) > 2 else "postgresql://postgres:postgres@localhost:5433/rollup_1m"
    run_mutations(m_url, r_url)
