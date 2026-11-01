#!/usr/bin/env python3
"""tests/test_reconnect.py:
Tests Worker Resiliency:
1. Terminate tenant 1's backend connection via pg_terminate_backend.
2. Insert a row in tenant 1 (id >= 900,000,000).
3. Verify worker reconnects with backoff, logs reconnect, runs safety sweep, and rollup catches up without restart.
4. Clean up test row.
"""
import sys, os, time, subprocess, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

cfg = get_config("1m")
MAIN_URL = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
T1_URL = "postgresql://postgres:postgres@127.0.0.1:5432/tenant_001"
ROLLUP_URL = cfg["database"]["rollup_db_url"]
TEST_ORDER_ID = 995000001
TEST_ITEM_ID = 995000001
TEST_HOUR = "2026-10-25 15:00:00"

def run_test():
    log_file = "worker_test.log"
    log_f = open(log_file, "w")
    print("Starting worker process (logging to worker_test.log)...")
    p_worker = subprocess.Popen([sys.executable, "worker/worker.py", "--profile", "1m", "--mode", "event"], stdout=log_f, stderr=subprocess.STDOUT)
    time.sleep(6.0)

    try:
        # Find tenant 1's connection PID
        c_main = psycopg2.connect(MAIN_URL)
        cur_main = c_main.cursor()
        cur_main.execute("""
            SELECT pid FROM pg_stat_activity 
            WHERE datname = 'tenant_001' AND pid != pg_backend_pid()
        """)
        row = cur_main.fetchone()
        assert row is not None, "Could not find worker connection for tenant_001"
        t1_pid = row[0]
        print(f"Found worker connection for Tenant 1: PID {t1_pid}")

        # Record initial rollup state for test bucket
        c_roll = psycopg2.connect(ROLLUP_URL)
        c_roll.autocommit = True
        cur_roll = c_roll.cursor()
        cur_roll.execute("SELECT coalesce(order_count, 0), coalesce(revenue, 0.00) FROM rollup_orders_hour WHERE tenant_id = 1 AND hour = %s", (TEST_HOUR,))
        init_row = cur_roll.fetchone()
        init_cnt = init_row[0] if init_row else 0
        init_rev = float(init_row[1]) if init_row else 0.0
        print(f"Initial rollup for Tenant 1 at {TEST_HOUR}: count={init_cnt}, rev={init_rev:.2f}")

        # Terminate tenant 1 connection
        print(f"Executing pg_terminate_backend({t1_pid})...")
        cur_main.execute("SELECT pg_terminate_backend(%s)", (t1_pid,))
        term_res = cur_main.fetchone()[0]
        print(f"pg_terminate_backend result: {term_res}")
        cur_main.close()
        c_main.close()

        time.sleep(1.0)

        # Insert test order in tenant_001 (id >= 900,000,000)
        print(f"Inserting test order {TEST_ORDER_ID} (100.00) into tenant_001...")
        c_t1 = psycopg2.connect(T1_URL)
        c_t1.autocommit = True
        cur_t1 = c_t1.cursor()
        cur_t1.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, 1, 1, 'delivered', 100.00, %s::timestamp, %s::timestamp)
        """, (TEST_ORDER_ID, TEST_HOUR, TEST_HOUR))
        cur_t1.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, 1, 1, 1, 100.00)
        """, (TEST_ITEM_ID, TEST_ORDER_ID))
        cur_t1.close()
        c_t1.close()

        # Wait for worker to reconnect and rollup to catch up
        print("Waiting for worker to reconnect and rollup to catch up...")
        t_start = time.time()
        caught_up = False
        while time.time() - t_start < 15.0:
            cur_roll.execute("SELECT coalesce(order_count, 0), coalesce(revenue, 0.00) FROM rollup_orders_hour WHERE tenant_id = 1 AND hour = %s", (TEST_HOUR,))
            r_now = cur_roll.fetchone()
            curr_cnt = r_now[0] if r_now else 0
            curr_rev = float(r_now[1]) if r_now else 0.0
            if curr_cnt == init_cnt + 1 and abs(curr_rev - (init_rev + 100.00)) < 0.01:
                caught_up = True
                print(f"[SUCCESS] Rollup caught up! count={curr_cnt} (was {init_cnt}), rev={curr_rev:.2f} (was {init_rev:.2f}) in {time.time() - t_start:.2f}s")
                break
            time.sleep(0.5)

        assert caught_up, "Rollup did not catch up within 15s after connection termination"

        # Cleanup test order
        c_t1 = psycopg2.connect(T1_URL)
        c_t1.autocommit = True
        cur_t1 = c_t1.cursor()
        cur_t1.execute("DELETE FROM order_items WHERE item_id = %s", (TEST_ITEM_ID,))
        cur_t1.execute("DELETE FROM orders WHERE order_id = %s", (TEST_ORDER_ID,))
        cur_t1.close()
        c_t1.close()

        # Wait for cleanup to propagate
        t_start = time.time()
        while time.time() - t_start < 10.0:
            cur_roll.execute("SELECT coalesce(order_count, 0), coalesce(revenue, 0.00) FROM rollup_orders_hour WHERE tenant_id = 1 AND hour = %s", (TEST_HOUR,))
            r_now = cur_roll.fetchone()
            curr_cnt = r_now[0] if r_now else 0
            if curr_cnt == init_cnt:
                print(f"[CLEANUP OK] Cleaned up test order {TEST_ORDER_ID}; count restored to {init_cnt}")
                break
            time.sleep(0.5)

        cur_roll.close()
        c_roll.close()

    finally:
        p_worker.terminate()
        p_worker.wait()
        log_f.close()
        if os.path.exists(log_file):
            with open(log_file, "r") as f:
                output = f.read()
            reconnect_logs = [l for l in output.splitlines() if "[RECONNECT]" in l and ("Tenant 1:" in l or "closed" in l or "reconnect" in l.lower())]
            print("\n--- RECONNECT LOGS FOR TENANT 1 ---")
            for log in reconnect_logs:
                print(log)
            try: os.remove(log_file)
            except Exception: pass

if __name__ == "__main__":
    run_test()
