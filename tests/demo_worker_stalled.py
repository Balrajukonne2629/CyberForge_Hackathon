#!/usr/bin/env python3
"""tests/demo_worker_stalled.py:
Demonstrates fail-closed behavior:
1. Verifies API initially reports worker_stalled = false
2. Injects a forced negative count into the outbox
3. Worker catches CHECK (order_count >= 0) violation, commits worker_stalled = true, and halts
4. Verifies API reports worker_stalled = true
5. Cleans up synthetic row and resumes worker
"""
import time, httpx, psycopg2, yaml

def run_demo():
    print("=== Worker Stalled Fail-Closed Demo ===")
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_1m"]
    api_url = "http://127.0.0.1:8000"

    client = httpx.Client(base_url=api_url)

    # 1. Check initial health
    r = client.get("/health")
    print("1. Initial API Health:", r.json())
    assert r.json()["worker_stalled"] is False

    # 2. Inject forced negative row in outbox on main-db
    print("\n2. Injecting forced negative count into main-db outbox...")
    main_conn = psycopg2.connect(main_url)
    main_conn.autocommit = True
    main_cur = main_conn.cursor()
    main_cur.execute("""
        INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
        VALUES ('order', 99, '2026-10-31 00:00:00', 'delivered', -999999, -999999.00)
        RETURNING id
    """)
    bad_id = main_cur.fetchone()[0]
    print(f"  Injected bad outbox row id={bad_id} (delta_count = -999999)")

    # 3. Wait for worker to encounter the row and halt
    print("\n3. Waiting for worker to hit the constraint and halt...")
    time.sleep(1.0)

    # 4. Check API health and endpoint response
    r = client.get("/health")
    health = r.json()
    print("4. API Health after forced error:", health)
    assert health["worker_stalled"] is True

    r_endpoint = client.get("/tenants/1/orders/count?from=2026-10-01&to=2026-10-30", headers={"X-Tenant-Key": "tenant-key-1"})
    print("  API response worker_stalled flag:", r_endpoint.json()["worker_stalled"])
    assert r_endpoint.json()["worker_stalled"] is True

    print("\n>>> DEMO SUCCESS: Fail-closed architecture verified! Worker stalled safely. <<<")

    # 5. Clean up bad row and reset worker_stalled
    print("\n5. Cleaning up bad outbox row and resetting state...")
    main_cur.execute("DELETE FROM outbox WHERE id = %s", (bad_id,))
    rollup_conn = psycopg2.connect(rollup_url)
    rollup_conn.autocommit = True
    rollup_cur = rollup_conn.cursor()
    rollup_cur.execute("UPDATE worker_state SET worker_stalled = false, stalled_reason = NULL WHERE id = 1")
    
    rollup_cur.close()
    rollup_conn.close()
    main_cur.close()
    main_conn.close()
    client.close()

if __name__ == "__main__":
    run_demo()
