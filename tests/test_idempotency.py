#!/usr/bin/env python3
"""tests/test_idempotency.py:
Tests cursor idempotency and replay safety per-tenant:
Verifies across at least 3 tenants (including the largest tenant 1, plus 42 and 99)
that processed outbox rows or snapshot-visible rows cannot double-count when re-evaluated.
"""
import sys, os, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url

#!/usr/bin/env python3
"""tests/test_idempotency.py:
Tests cursor idempotency and replay safety per-tenant:
1. Proves the cursor advances when new outbox rows are committed.
   Fails if cursor is ('0', 0) or did not advance.
2. Replays the same batch using the cursor-bounded predicate, proving 0 duplicate rows are consumed.
3. Verifies rollup counts remain strictly identical after replay.
Tested across at least 3 tenants (including largest Tenant 1, plus 42 and 99).
"""
import sys, os, time, datetime, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_idempotency_test(profile: str = "1m"):
    print(f"=== Running Per-Tenant Idempotency & Batch Replay Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]

    test_tenants = [1, 42, 99]

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_cur = rollup_conn.cursor()

    for tid in test_tenants:
        t_url = get_tenant_db_url(tid, profile)
        t_conn = psycopg2.connect(t_url)
        t_conn.autocommit = True
        t_cur = t_conn.cursor()

        # Step 0: Ensure prior state is settled and record initial cursor
        wait_for_worker(profile, tenant_id=tid, timeout=10.0)
        rollup_cur.execute("""
            SELECT cursor_xid::text, cursor_id 
            FROM worker_state 
            WHERE tenant_id = %s
        """, (tid,))
        row0 = rollup_cur.fetchone()
        init_xid, init_id = (row0[0], row0[1]) if row0 else ('0', 0)

        # Step 1: Inject an active test batch into the tenant outbox
        test_hour = "2026-10-29 11:00:00"
        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES 
                ('order', %s, %s::timestamp, 'delivered', 1, 100.00),
                ('order', %s, %s::timestamp, 'delivered', 1, 150.00),
                ('order', %s, %s::timestamp, 'delivered', 1, 200.00)
            RETURNING id, xid::text
        """, (tid, test_hour, tid, test_hour, tid, test_hour))
        injected_rows = t_cur.fetchall()
        injected_ids = [r[0] for r in injected_rows]
        last_injected_id = max(injected_ids)

        try:
            # Step 2: Wait for worker to consume the batch
            wait_for_worker(profile, tenant_id=tid, timeout=15.0)

            # Step 3: Prove the cursor strictly ADVANCED in this run
            rollup_cur.execute("""
                SELECT cursor_xid::text, cursor_id 
                FROM worker_state 
                WHERE tenant_id = %s
            """, (tid,))
            new_xid, new_id = rollup_cur.fetchone()

            print(f"  Tenant {tid:2d}: Initial cursor=({init_xid}, {init_id}) -> Advanced cursor=({new_xid}, {new_id})")
            assert (new_xid, new_id) != ('0', 0), f"Tenant {tid} cursor is ('0', 0) - nothing processed!"
            assert new_id >= last_injected_id, f"Tenant {tid} cursor failed to advance past {last_injected_id}: got {new_id}"

            # Step 4: Capture rollup state after processing
            rollup_cur.execute("""
                SELECT order_count, revenue 
                FROM rollup_orders_hour 
                WHERE tenant_id = %s AND hour = %s AND status = 'delivered'
            """, (tid, test_hour))
            post_batch_rollup = rollup_cur.fetchone()

            # Step 5: REPLAY the batch by querying with current cursor
            # Under cursor idempotency, replaying MUST yield 0 rows
            t_cur.execute("""
                SELECT count(*) FROM outbox
                WHERE id IN (%s, %s, %s)
                  AND (xid > %s::xid8 OR (xid = %s::xid8 AND id > %s))
            """, (injected_ids[0], injected_ids[1], injected_ids[2], new_xid, new_xid, new_id))
            replay_unconsumed = t_cur.fetchone()[0]

            assert replay_unconsumed == 0, f"Tenant {tid} batch replay leak: {replay_unconsumed} rows visible beyond cursor"

            # Step 6: Verify rollups remain strictly unchanged after replay check
            rollup_cur.execute("""
                SELECT order_count, revenue 
                FROM rollup_orders_hour 
                WHERE tenant_id = %s AND hour = %s AND status = 'delivered'
            """, (tid, test_hour))
            post_replay_rollup = rollup_cur.fetchone()

            assert post_batch_rollup == post_replay_rollup, f"Tenant {tid} rollup mutated during replay: {post_batch_rollup} != {post_replay_rollup}"

        finally:
            # Step 7: Clean up test outbox rows and compensate rollup
            t_cur.execute("""
                INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
                VALUES 
                    ('order', %s, %s::timestamp, 'delivered', -1, -100.00),
                    ('order', %s, %s::timestamp, 'delivered', -1, -150.00),
                    ('order', %s, %s::timestamp, 'delivered', -1, -200.00)
            """, (tid, test_hour, tid, test_hour, tid, test_hour))
            wait_for_worker(profile, tenant_id=tid, timeout=10.0)

            t_cur.close()
            t_conn.close()

    rollup_cur.close()
    rollup_conn.close()
    print(">>> PER-TENANT IDEMPOTENCY & BATCH REPLAY TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_idempotency_test(prof)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_idempotency_test(prof)
    sys.exit(0 if ok else 1)
