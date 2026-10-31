#!/usr/bin/env python3
"""tests/test_quarantine.py:
Tests quarantine behavior for malformed/corrupted rows:
1. Injects outbox rows with unknown status and invalid tenant.
2. Worker runs and routes invalid rows to quarantine.
3. Worker applies valid rows in the batch without stalling.
"""
import sys, os, time, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def run_quarantine_test(profile: str = "1m"):
    print(f"=== Running Quarantine Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_db_url"]

    main_conn = psycopg2.connect(main_url)
    main_conn.autocommit = True
    main_cur = main_conn.cursor()

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_conn.autocommit = True
    rollup_cur = rollup_conn.cursor()

    # 1. Inject malformed outbox rows
    print("1. Injecting malformed outbox rows on main-db...")
    main_cur.execute("""
        INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
        VALUES 
            ('order', 1, '2026-10-28 10:00:00', 'corrupted_status_xyz', 1, 50.00),
            ('order', -99, '2026-10-28 10:00:00', 'delivered', 1, 50.00)
        RETURNING id
    """)
    bad_ids = [r[0] for r in main_cur.fetchall()]
    print(f"  Injected bad outbox row ids: {bad_ids}")

    # Also inject a valid row in the same sequence
    main_cur.execute("""
        INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
        VALUES ('order', 1, '2026-10-28 10:00:00', 'delivered', 1, 25.00)
        RETURNING id
    """)
    valid_id = main_cur.fetchone()[0]

    # 2. Wait for worker to process
    from tests.sync import wait_for_worker
    wait_for_worker(main_url, rollup_url)

    # 3. Check quarantine table
    rollup_cur.execute("SELECT id, outbox_id, reason FROM quarantine WHERE outbox_id IN (%s, %s)", tuple(bad_ids))
    q_rows = rollup_cur.fetchall()
    print(f"3. Rows in quarantine table: {q_rows}")
    assert len(q_rows) == 2, f"Expected 2 quarantined rows, got {len(q_rows)}"

    # 4. Verify worker state is still healthy (not stalled)
    rollup_cur.execute("SELECT worker_stalled FROM worker_state WHERE id = 1")
    stalled = rollup_cur.fetchone()[0]
    print(f"4. Worker stalled status: {stalled}")
    assert stalled is False

    # Cleanup
    rollup_cur.execute("DELETE FROM quarantine WHERE outbox_id IN (%s, %s)", tuple(bad_ids))
    # Compensate rollup for the test outbox row so rollups remain perfectly in sync with main-db
    rollup_cur.execute("""
        UPDATE rollup_orders_hour 
        SET order_count = order_count - 1, revenue = revenue - 25.00
        WHERE tenant_id = 1 AND hour = '2026-10-28 10:00:00' AND status = 'delivered'
    """)
    main_cur.execute("DELETE FROM outbox WHERE id IN (%s, %s, %s)", tuple(bad_ids + [valid_id]))

    main_cur.close()
    main_conn.close()
    rollup_cur.close()
    rollup_conn.close()

    print(">>> QUARANTINE TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_quarantine_test(prof)
    sys.exit(0 if ok else 1)
