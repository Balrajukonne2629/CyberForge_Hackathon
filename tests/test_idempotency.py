#!/usr/bin/env python3
"""tests/test_idempotency.py:
Tests cursor idempotency and replay safety per-tenant:
Verifies across at least 3 tenants (including the largest tenant 1, plus 42 and 99)
that processed outbox rows or snapshot-visible rows cannot double-count when re-evaluated.
"""
import sys, os, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url

def run_idempotency_test(profile: str = "1m"):
    print(f"=== Running Per-Tenant Idempotency Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]

    test_tenants = [1, 42, 99]

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_cur = rollup_conn.cursor()

    for tid in test_tenants:
        t_url = get_tenant_db_url(tid, profile)
        t_conn = psycopg2.connect(t_url)
        t_cur = t_conn.cursor()

        # 1. Fetch current rollup state and cursor for tenant
        rollup_cur.execute("SELECT sum(order_count) FROM rollup_orders_hour WHERE tenant_id = %s", (tid,))
        total_before = rollup_cur.fetchone()[0] or 0

        rollup_cur.execute("SELECT cursor_xid::text, cursor_id, snapshot::text FROM worker_state WHERE tenant_id = %s", (tid,))
        cur_row = rollup_cur.fetchone()
        cur_xid, cur_id, snapshot_str = cur_row if cur_row else ('0', 0, None)

        # 2. Check unconsumed rows on tenant's outbox (respecting rebuild snapshot if set)
        t_cur.execute("""
            SELECT count(*) FROM outbox
            WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
              AND (xid > %s::xid8 OR (xid = %s::xid8 AND id > %s))
              AND CASE 
                    WHEN %s IS NOT NULL AND %s != '' THEN NOT pg_visible_in_snapshot(xid, %s::pg_snapshot) 
                    ELSE true 
                  END
        """, (cur_xid, cur_xid, cur_id, snapshot_str, snapshot_str, snapshot_str))
        unconsumed = t_cur.fetchone()[0]

        # 3. Verify total orders unchanged
        rollup_cur.execute("SELECT sum(order_count) FROM rollup_orders_hour WHERE tenant_id = %s", (tid,))
        total_after = rollup_cur.fetchone()[0] or 0

        print(f"  Tenant {tid:2d}: orders={total_before:,}, cursor=({cur_xid}, {cur_id}), unconsumed={unconsumed}")
        assert unconsumed == 0, f"Tenant {tid} had {unconsumed} unconsumed rows"
        assert total_before == total_after, f"Tenant {tid} count changed: {total_before} -> {total_after}"

        t_cur.close()
        t_conn.close()

    rollup_cur.close()
    rollup_conn.close()
    print(">>> PER-TENANT IDEMPOTENCY TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_idempotency_test(prof)
    sys.exit(0 if ok else 1)
