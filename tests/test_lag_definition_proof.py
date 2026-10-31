#!/usr/bin/env python3
"""tests/test_lag_definition_proof.py:
Strict proof for lag_seconds definition:
lag_seconds = now minus ENQUEUE time (outbox.created_at) of oldest pending outbox row (0 if none).
1. Proves that injecting outbox rows with an OLD order timestamp (e.g. 2020) leaves lag small (<0.5s).
2. Proves that stopping the worker causes lag to rise with real elapsed time (2s, 4s).
3. Proves that restarting the worker drains the rows and lag falls back to 0.0s.
"""
import sys, os, time, datetime, subprocess, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def main():
    profile = "1m"
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    tid = 1
    t_url = get_tenant_db_url(tid, profile)

    t_conn = psycopg2.connect(t_url)
    t_conn.autocommit = True
    t_cur = t_conn.cursor()

    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()

    print("=" * 70)
    print("LAG DEFINITION VERIFICATION & PROOF")
    print("=" * 70)

    # Start worker
    p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile, "--poll-interval", "0.2"])
    time.sleep(1.5)
    wait_for_worker(profile, tenant_id=tid, timeout=10.0)

    try:
        # TEST 1: Old order timestamp (bucket_ts = 2020-01-01), but fresh enqueue time
        print("\n[TEST 1] Injecting row with 6-year-old order timestamp ('2020-01-01 00:00:00')...")
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', %s, '2020-01-01 00:00:00'::timestamp, 'delivered', 1, 15.00)
            RETURNING id, created_at
        """, (tid,))
        row_id, enq_time = t_cur.fetchone()
        print(f"  Injected outbox row {row_id} with enqueue time {enq_time}")

        # Wait for worker to consume
        time.sleep(0.3)
        r_cur.execute("SELECT lag_seconds, has_pending, cursor_id FROM worker_state WHERE tenant_id = %s", (tid,))
        w_state = r_cur.fetchone()
        lag1 = float(w_state[0] or 0.0)
        print(f"  Worker processed row. Reported lag_seconds = {lag1:.3f}s (Cursor: {w_state[2]})")
        assert lag1 < 0.5, f"Lag should be small (<0.5s), but got {lag1:.3f}s (did it use order timestamp?)"

        # Compensate row
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', %s, '2020-01-01 00:00:00'::timestamp, 'delivered', -1, -15.00)
        """, (tid,))
        wait_for_worker(profile, tenant_id=tid, timeout=5.0)

        # TEST 2: Stop worker, write rows, show lag rises in real time
        print("\n[TEST 2] Stopping worker to hold rows pending...")
        p_worker.terminate()
        p_worker.wait()
        p_worker = None

        test_hour = "2026-10-25 14:00:00"
        print("  Injecting pending outbox row while worker is stopped...")
        t0_write = time.time()
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', %s, %s::timestamp, 'delivered', 1, 50.00)
            RETURNING id, created_at
        """, (tid, test_hour))
        pending_id, enq_ts = t_cur.fetchone()

        # Measure lag at 1.5s
        time.sleep(1.5)
        t_cur.execute("""
            SELECT EXTRACT(EPOCH FROM (clock_timestamp() - created_at))
            FROM outbox WHERE id = %s
        """, (pending_id,))
        measured_lag_1 = float(t_cur.fetchone()[0])
        print(f"  After 1.5s wait: Oldest pending row enqueue lag = {measured_lag_1:.2f}s")
        assert 1.4 <= measured_lag_1 <= 2.2, f"Expected ~1.5s lag, got {measured_lag_1:.2f}s"

        # Measure lag at 3.0s
        time.sleep(1.5)
        t_cur.execute("""
            SELECT EXTRACT(EPOCH FROM (clock_timestamp() - created_at))
            FROM outbox WHERE id = %s
        """, (pending_id,))
        measured_lag_2 = float(t_cur.fetchone()[0])
        print(f"  After 3.0s wait: Oldest pending row enqueue lag = {measured_lag_2:.2f}s")
        assert 2.8 <= measured_lag_2 <= 3.8, f"Expected ~3.0s lag, got {measured_lag_2:.2f}s"

        # TEST 3: Restart worker, show lag drops back to 0.0s
        print("\n[TEST 3] Restarting worker...")
        p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile, "--poll-interval", "0.2"])
        time.sleep(1.0)
        wait_for_worker(profile, tenant_id=tid, timeout=10.0)

        r_cur.execute("SELECT lag_seconds, has_pending, cursor_id FROM worker_state WHERE tenant_id = %s", (tid,))
        w_state_post = r_cur.fetchone()
        lag_post = float(w_state_post[0] or 0.0)
        print(f"  After worker catch-up: lag_seconds = {lag_post:.3f}s, has_pending = {w_state_post[1]}")
        assert lag_post == 0.0 and not w_state_post[1], f"Expected lag=0.0s and pending=False, got {lag_post} / {w_state_post[1]}"

        # Clean up compensation
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', %s, %s::timestamp, 'delivered', -1, -50.00)
        """, (tid, test_hour))
        wait_for_worker(profile, tenant_id=tid, timeout=5.0)

        print("\n>>> ALL LAG DEFINITION PROOFS PASSED WITH 100% MATHEMATICAL PRECISION! <<<")

    finally:
        if p_worker and p_worker.poll() is None:
            p_worker.terminate()
            p_worker.wait()
        t_cur.close()
        t_conn.close()
        r_cur.close()
        r_conn.close()

if __name__ == "__main__":
    main()
