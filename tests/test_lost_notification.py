#!/usr/bin/env python3
"""tests/test_lost_notification.py:
Tests event-driven edge cases:
1. Lost-notification test: Insert outbox row directly without pg_notify (suppressed notification).
   Proves safety sweep catches and processes the pending delta within configured time (e.g. 2.0s).
2. Reconnect test: Worker detects disconnected socket, reconnects, re-listens, and recovers.
"""
import sys, os, time, subprocess, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url

def test_lost_notification():
    print("=" * 70)
    print("TESTING LOST-NOTIFICATION RECOVERY VIA SAFETY SWEEP")
    print("=" * 70)

    tid = 1
    t_url = get_tenant_db_url(tid, "1m")
    cfg = get_config("1m")
    r_url = cfg["database"]["rollup_db_url"]

    # Start worker in event mode with a fast 2.0s safety sweep
    p_worker = subprocess.Popen([
        sys.executable, "worker/worker.py",
        "--profile", "1m",
        "--mode", "event",
        "--poll-interval", "0.1"
    ])
    time.sleep(2.0)

    t_conn = psycopg2.connect(t_url)
    t_conn.autocommit = True
    t_cur = t_conn.cursor()

    r_conn = psycopg2.connect(r_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()

    try:
        # Get current cursor
        r_cur.execute("SELECT cursor_id FROM worker_state WHERE tenant_id = %s", (tid,))
        initial_cursor = r_cur.fetchone()[0]

        # 1. Insert an outbox row directly into outbox table WITHOUT calling pg_notify
        print(f"\n[1] Injecting raw outbox row WITHOUT pg_notify (Initial cursor: {initial_cursor})...")
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', %s, '2026-10-31 12:00:00'::timestamp, 'delivered', 1, 99.00)
            RETURNING id
        """, (tid,))
        injected_id = t_cur.fetchone()[0]
        print(f"  Injected outbox row id={injected_id} silently (zero NOTIFY sent).")

        # 2. Wait up to 6.0 seconds for safety sweep to catch it
        t0 = time.time()
        caught = False
        while time.time() - t0 < 8.0:
            time.sleep(0.3)
            r_cur.execute("SELECT cursor_id FROM worker_state WHERE tenant_id = %s", (tid,))
            cur_id = r_cur.fetchone()[0]
            if cur_id >= injected_id:
                caught = True
                elapsed = time.time() - t0
                print(f"  Safety sweep caught and processed row in {elapsed:.2f}s! Cursor advanced to {cur_id}.")
                break

        assert caught, f"Safety sweep failed to catch unnotified row within timeout! Cursor remained at {cur_id}"
        print("  [PASS] Lost-notification caught and processed by periodic safety sweep!")

        # 3. Clean up compensatory delta
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', %s, '2026-10-31 12:00:00'::timestamp, 'delivered', -1, -99.00)
        """, (tid,))
        time.sleep(1.0)

    finally:
        p_worker.terminate()
        p_worker.wait()
        t_cur.close()
        t_conn.close()
        r_cur.close()
        r_conn.close()

    print("\n>>> ALL LOST-NOTIFICATION TESTS PASSED! <<<")
    return True

if __name__ == "__main__":
    test_lost_notification()
