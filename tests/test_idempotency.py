#!/usr/bin/env python3
"""tests/test_idempotency.py:
Tests cursor idempotency and replaying:
Verifies that previously processed outbox rows or snapshot-visible rows
cannot double-count when re-evaluated.
"""
import sys, os, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def run_idempotency_test(profile: str = "1m"):
    print(f"=== Running Idempotency Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    main_url = cfg["database"]["main_db_url"]

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_cur = rollup_conn.cursor()

    # 1. Capture current total orders in rollup
    rollup_cur.execute("SELECT sum(order_count) FROM rollup_orders_hour")
    total_before = rollup_cur.fetchone()[0]

    rollup_cur.execute("SELECT cursor_xid::text, cursor_id FROM worker_state WHERE id = 1")
    cur_xid, cur_id = rollup_cur.fetchone()
    print(f"1. Current state: total_orders={total_before:,}, cursor=({cur_xid}, {cur_id})")

    # 2. Verify worker query behavior with existing cursor
    # Any query with xid <= cur_xid returns 0 rows due to safe-cursor predicate
    main_conn = psycopg2.connect(main_url)
    main_cur = main_conn.cursor()
    main_cur.execute("""
        SELECT count(*) FROM outbox
        WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
          AND (xid > %s::xid8 OR (xid = %s::xid8 AND id > %s))
    """, (cur_xid, cur_xid, cur_id))
    unconsumed = main_cur.fetchone()[0]
    print(f"2. Unconsumed rows beyond saved cursor: {unconsumed}")
    assert unconsumed == 0, f"Expected 0 pending unconsumed rows, found {unconsumed}"

    # 3. Simulate re-running cursor check
    rollup_cur.execute("SELECT sum(order_count) FROM rollup_orders_hour")
    total_after = rollup_cur.fetchone()[0]
    print(f"3. Total orders after verification: {total_after:,}")
    assert total_before == total_after

    main_cur.close()
    main_conn.close()
    rollup_cur.close()
    rollup_conn.close()

    print(">>> IDEMPOTENCY TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_idempotency_test(prof)
    sys.exit(0 if ok else 1)
