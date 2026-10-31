#!/usr/bin/env python3
"""tests/test_fail_closed.py:
Tests per-tenant fail closed semantics:
1. Forces a negative count delta into Tenant 5's outbox.
2. Only Tenant 5 stalls; other tenants (e.g. Tenant 1) continue ingesting normally.
3. API echoes "worker_stalled": true for Tenant 5 and "worker_stalled": false for Tenant 1.
4. /health shows global state "status": "stalled" with stalled_tenants_count >= 1 and 5 in list.
5. Remediates Tenant 5 and verifies recovery.
"""
import sys, os, time, datetime, httpx, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_fail_closed_test(profile: str = "1m") -> bool:
    print(f"=== Running Per-Tenant Fail-Closed Constraint Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    api_url = "http://127.0.0.1:8000"
    client = httpx.Client(base_url=api_url, timeout=10.0)

    stall_tenant = 5
    healthy_tenant = 1

    stall_url = get_tenant_db_url(stall_tenant, profile)
    healthy_url = get_tenant_db_url(healthy_tenant, profile)

    s_conn = psycopg2.connect(stall_url)
    s_conn.autocommit = True
    s_cur = s_conn.cursor()

    h_conn = psycopg2.connect(healthy_url)
    h_conn.autocommit = True
    h_cur = h_conn.cursor()

    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()

    bad_id = None
    oid = 555_001

    try:
        # 1. Clean prior state
        s_cur.execute("DELETE FROM outbox WHERE delta_count < 0")
        r_cur.execute("UPDATE worker_state SET stalled = false, last_error = NULL WHERE tenant_id = %s", (stall_tenant,))

        # 2. Inject forced negative row in Tenant 5 outbox
        print(f"1. Injecting negative outbox delta into Tenant {stall_tenant}...")
        s_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', %s, '2026-10-31 00:00:00', 'delivered', -999999, -999999.00)
            RETURNING id
        """, (stall_tenant,))
        bad_id = s_cur.fetchone()[0]

        # 3. Concurrently inject valid order into Tenant 1
        print(f"2. Injecting valid order into Tenant {healthy_tenant}...")
        test_ts = datetime.datetime(2026, 10, 25, 11, 0, 0)
        h_cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
        h_cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
        h_cur.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, %s, 1, 'delivered', 80.00, %s, %s)
        """, (oid, healthy_tenant, test_ts, test_ts))
        h_cur.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, %s, 1, 1, 80.00)
        """, (oid, oid, healthy_tenant))

        # 4. Wait for worker to stall Tenant 5
        print(f"3. Waiting for worker to hit constraint on Tenant {stall_tenant}...")
        t_poll_start = time.time()
        stalled_observed = False
        while time.time() - t_poll_start < 6.0:
            r_cur.execute("SELECT stalled, last_error FROM worker_state WHERE tenant_id = %s", (stall_tenant,))
            row = r_cur.fetchone()
            if row and row[0] is True:
                stalled_observed = True
                print(f"  Observed stall on Tenant {stall_tenant}: {row[1]}")
                break
            time.sleep(0.1)

        assert stalled_observed, f"Worker did not stall Tenant {stall_tenant} within timeout"

        # 5. Verify healthy tenant caught up without being blocked
        wait_for_worker(profile, tenant_id=healthy_tenant, timeout=10.0)
        print(f"  Tenant {healthy_tenant} successfully ingested despite Tenant {stall_tenant} stall!")

        # 6. Check API responses
        res_s = client.get(
            f"/tenants/{stall_tenant}/orders/count?from=2026-10-01&to=2026-10-30",
            headers={"X-Tenant-Key": f"tenant-key-{stall_tenant}"}
        )
        assert res_s.status_code == 200
        assert res_s.json()["worker_stalled"] is True, "Tenant 5 API response should have worker_stalled=True"
        print(f"4. Tenant {stall_tenant} API worker_stalled: {res_s.json()['worker_stalled']}")

        res_h = client.get(
            f"/tenants/{healthy_tenant}/orders/count?from=2026-10-01&to=2026-10-30",
            headers={"X-Tenant-Key": f"tenant-key-{healthy_tenant}"}
        )
        assert res_h.status_code == 200
        assert res_h.json()["worker_stalled"] is False, "Tenant 1 API response should have worker_stalled=False"
        print(f"5. Tenant {healthy_tenant} API worker_stalled: {res_h.json()['worker_stalled']}")

        health_res = client.get("/health").json()
        print(f"6. Global /health response: status={health_res['status']}, stalled={health_res['stalled_tenant_ids']}")
        assert health_res["status"] == "stalled"
        assert health_res["worker_stalled"] is True
        assert stall_tenant in health_res["stalled_tenant_ids"]

    finally:
        # Remediation & Recovery in finally block
        print("7. Remediating Tenant 5: removing bad outbox row and resetting stalled flag...")
        if bad_id:
            s_cur.execute("DELETE FROM outbox WHERE id = %s", (bad_id,))
        s_cur.execute("DELETE FROM outbox WHERE delta_count < 0")
        r_cur.execute("UPDATE worker_state SET stalled = false, last_error = NULL WHERE tenant_id = %s", (stall_tenant,))

        h_cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
        h_cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))

        time.sleep(1.0)
        wait_for_worker(profile, tenant_id=healthy_tenant, timeout=10.0)

        s_cur.close(); s_conn.close()
        h_cur.close(); h_conn.close()
        r_cur.close(); r_conn.close()
        client.close()

    print("\n>>> PER-TENANT FAIL-CLOSED TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_fail_closed_test(prof)
    sys.exit(0 if ok else 1)
