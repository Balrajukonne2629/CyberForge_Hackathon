#!/usr/bin/env python3
"""tests/test_fail_closed.py:
Tests fail closed behavior on check constraint violation.
"""
import sys, os, subprocess, time, httpx, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def run_fail_closed_test(profile: str = "1m"):
    print(f"=== Running Fail-Closed Constraint Test ({profile.upper()}) ===")
    cfg = get_config(profile)
    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_db_url"]
    api_url = "http://127.0.0.1:8000"
    client = httpx.Client(base_url=api_url)

    # 1. Start worker in separate subprocess
    p = subprocess.Popen(["py", "-3.12", "worker/worker.py", main_url, rollup_url])
    time.sleep(1.0)

    # 2. Inject forced negative row in outbox on main-db
    main_conn = psycopg2.connect(main_url)
    main_conn.autocommit = True
    main_cur = main_conn.cursor()
    main_cur.execute("""
        INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
        VALUES ('order', 99, '2026-10-31 00:00:00', 'delivered', -999999, -999999.00)
        RETURNING id
    """)
    bad_id = main_cur.fetchone()[0]
    print(f"1. Injected negative outbox row id={bad_id}")

    # 3. Wait for worker to halt
    time.sleep(1.0)
    p.wait()

    # 4. Check API health
    r = client.get("/health").json()
    print(f"2. Health endpoint response: {r}")
    assert r["worker_stalled"] is True

    # 5. Clean up
    main_cur.execute("DELETE FROM outbox WHERE id = %s", (bad_id,))
    rollup_conn = psycopg2.connect(rollup_url)
    rollup_conn.autocommit = True
    rollup_cur = rollup_conn.cursor()
    rollup_cur.execute("UPDATE worker_state SET worker_stalled = false, stalled_reason = NULL WHERE id = 1")

    main_cur.close()
    main_conn.close()
    rollup_cur.close()
    rollup_conn.close()
    client.close()

    print(">>> FAIL-CLOSED TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_fail_closed_test(prof)
    sys.exit(0 if ok else 1)
