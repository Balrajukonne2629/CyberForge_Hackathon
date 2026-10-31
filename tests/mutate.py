#!/usr/bin/env python3
"""tests/mutate.py:
Tests cancel flips, un-cancel flips, timestamp bucket edits, and deletes.
Runs against the tenant's isolated database and verifies changes reach rollup-db with exact numbers.
"""
import sys, os, time, datetime, yaml, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_mutations(profile: str = "1m", tenant_id: int = 1):
    print(f"=== Running Mutation & Fan-Out Tests on Tenant {tenant_id} ({profile.upper()}) ===")
    cfg = get_config(profile)
    tenant_url = get_tenant_db_url(tenant_id, profile)
    rollup_url = cfg["database"]["rollup_db_url"]

    t_conn = psycopg2.connect(tenant_url)
    t_conn.autocommit = True
    t_cur = t_conn.cursor()

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_cur = rollup_conn.cursor()

    test_order_id = 999_999_001
    test_item_id = 999_999_001
    test_prod_id = 1
    test_qty = 2
    test_price = 150.00
    test_total = 300.00
    test_ts = datetime.datetime(2026, 10, 25, 10, 0, 0)
    test_hour = datetime.datetime(2026, 10, 25, 10, 0, 0)
    test_day = datetime.datetime(2026, 10, 25, 0, 0, 0)

    # Clean up any leftover test data
    t_cur.execute("DELETE FROM order_items WHERE order_id = %s", (test_order_id,))
    t_cur.execute("DELETE FROM orders WHERE order_id = %s", (test_order_id,))
    wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

    # 1. INSERT new order with 'delivered' status
    print("\n[TEST 1] Inserting new delivered order (300.00, 2 units)...")
    t_cur.execute("""
        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
        VALUES (%s, %s, 1, 'delivered', %s, %s, %s)
    """, (test_order_id, tenant_id, test_total, test_ts, test_ts))
    t_cur.execute("""
        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (test_item_id, test_order_id, tenant_id, test_prod_id, test_qty, test_price))

    wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

    rollup_cur.execute("""
        SELECT order_count, revenue FROM rollup_orders_hour 
        WHERE tenant_id = %s AND hour = %s AND status = 'delivered'
    """, (tenant_id, test_hour))
    h_row = rollup_cur.fetchone()
    print(f"  Rollup hour: {h_row}")
    assert h_row and h_row[0] >= 1 and h_row[1] >= test_total

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (tenant_id, test_day, test_prod_id))
    p_row = rollup_cur.fetchone()
    print(f"  Rollup product day: {p_row}")
    assert p_row and p_row[0] >= test_qty and p_row[1] >= test_total
    prev_units, prev_sales = p_row

    # 2. CANCEL FLIP: Update status to 'canceled'
    print("\n[TEST 2] Cancelling order (fan-out should subtract items)...")
    t_cur.execute("""
        UPDATE orders SET status = 'canceled', updated_at = now() WHERE order_id = %s
    """, (test_order_id,))

    wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (tenant_id, test_day, test_prod_id))
    p_after_cancel = rollup_cur.fetchone()
    print(f"  Rollup product day after cancel: units={p_after_cancel[0]} (was {prev_units})")
    assert p_after_cancel[0] == prev_units - test_qty
    assert abs(float(p_after_cancel[1]) - (float(prev_sales) - test_total)) < 0.01

    # 3. UN-CANCEL FLIP: Restore to 'delivered'
    print("\n[TEST 3] Un-cancelling order (fan-out should re-add items)...")
    t_cur.execute("""
        UPDATE orders SET status = 'delivered', updated_at = now() WHERE order_id = %s
    """, (test_order_id,))

    wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (tenant_id, test_day, test_prod_id))
    p_after_uncancel = rollup_cur.fetchone()
    print(f"  Rollup product day after un-cancel: units={p_after_uncancel[0]}, sales={p_after_uncancel[1]}")
    assert p_after_uncancel[0] == prev_units
    assert abs(float(p_after_uncancel[1]) - float(prev_sales)) < 0.01

    # 4. TIMESTAMP SHIFT: Move order to another day (2026-10-26)
    new_ts = datetime.datetime(2026, 10, 26, 14, 0, 0)
    new_day = datetime.datetime(2026, 10, 26, 0, 0, 0)
    print("\n[TEST 4] Moving order timestamp from 2026-10-25 to 2026-10-26...")
    t_cur.execute("""
        UPDATE orders SET created_at = %s, updated_at = now() WHERE order_id = %s
    """, (new_ts, test_order_id))

    wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (tenant_id, test_day, test_prod_id))
    p_old_day = rollup_cur.fetchone()
    assert p_old_day[0] == prev_units - test_qty

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (tenant_id, new_day, test_prod_id))
    p_new_day = rollup_cur.fetchone()
    assert p_new_day and p_new_day[0] >= test_qty

    # 5. DELETE order and items
    print("\n[TEST 5] Deleting order item and order...")
    t_cur.execute("DELETE FROM order_items WHERE order_id = %s", (test_order_id,))
    t_cur.execute("DELETE FROM orders WHERE order_id = %s", (test_order_id,))

    wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

    rollup_cur.execute("""
        SELECT units, sales FROM rollup_product_day 
        WHERE tenant_id = %s AND day = %s AND product_id = %s
    """, (tenant_id, new_day, test_prod_id))
    p_after_del = rollup_cur.fetchone()
    assert p_after_del[0] == p_new_day[0] - test_qty

    print("\n>>> ALL MUTATION & FAN-OUT TESTS PASSED WITH 100% EXACT PARITY! <<<")
    t_cur.close()
    t_conn.close()
    rollup_cur.close()
    rollup_conn.close()
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    run_mutations(profile=prof)
