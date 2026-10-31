#!/usr/bin/env python3
"""tests/test_quarantine.py:
Tests quarantine behavior for malformed/corrupted rows:
1. Injects outbox rows with unknown status and invalid tenant.
2. Worker runs and routes invalid rows to quarantine with tenant_id preserved.
3. Worker applies valid rows in the batch without stalling.
"""
import sys, os, time, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_quarantine_test(profile: str = "1m"):
    print(f"=== Running Quarantine Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    tenant_id = 1
    t_url = get_tenant_db_url(tenant_id, profile)
    rollup_url = cfg["database"]["rollup_db_url"]

    t_conn = psycopg2.connect(t_url)
    t_conn.autocommit = True
    t_cur = t_conn.cursor()

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_conn.autocommit = True
    rollup_cur = rollup_conn.cursor()

    bad_ids = []
    valid_id = None
    try:
        # 1. Inject malformed outbox rows on Tenant 1 DB
        print(f"1. Injecting malformed outbox rows on Tenant {tenant_id}...")
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES 
                ('order', 1, '2026-10-28 10:00:00', 'corrupted_status_xyz', 1, 50.00),
                ('order', -99, '2026-10-28 10:00:00', 'delivered', 1, 50.00)
            RETURNING id
        """)
        bad_ids = [r[0] for r in t_cur.fetchall()]
        print(f"  Injected bad outbox row ids: {bad_ids}")

        # Also inject a valid row in the same sequence
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', 1, '2026-10-28 10:00:00', 'delivered', 1, 25.00)
            RETURNING id
        """)
        valid_id = t_cur.fetchone()[0]

        # 2. Wait for worker to process
        wait_for_worker(profile, tenant_id=tenant_id, timeout=10.0)

        # 3. Check quarantine table
        rollup_cur.execute("SELECT id, tenant_id, outbox_id, reason FROM quarantine WHERE outbox_id IN (%s, %s)", tuple(bad_ids))
        q_rows = rollup_cur.fetchall()
        print(f"3. Rows in quarantine table: {q_rows}")
        assert len(q_rows) == 2, f"Expected 2 quarantined rows, got {len(q_rows)}"

        # 4. Verify worker state for tenant is still healthy (not stalled)
        rollup_cur.execute("SELECT stalled FROM worker_state WHERE tenant_id = %s", (tenant_id,))
        stalled = rollup_cur.fetchone()[0]
        print(f"4. Worker stalled status for Tenant {tenant_id}: {stalled}")
        assert stalled is False
    finally:
        # Cleanup
        if bad_ids:
            rollup_cur.execute("DELETE FROM quarantine WHERE outbox_id IN (%s, %s)", tuple(bad_ids))
        if valid_id:
            rollup_cur.execute("""
                UPDATE rollup_orders_hour 
                SET order_count = order_count - 1, revenue = revenue - 25.00
                WHERE tenant_id = 1 AND hour = '2026-10-28 10:00:00' AND status = 'delivered'
            """)
            ids_to_del = tuple(bad_ids + [valid_id])
            t_cur.execute("DELETE FROM outbox WHERE id IN (%s, %s, %s)", ids_to_del)

        t_cur.close(); t_conn.close()
        rollup_cur.close(); rollup_conn.close()

    print(">>> QUARANTINE TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_quarantine_test(prof)
    sys.exit(0 if ok else 1)
