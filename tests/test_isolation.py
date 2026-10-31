#!/usr/bin/env python3
"""tests/test_isolation.py:
Tests strict multi-tenant isolation:
1. Tenant A key on Tenant B path -> 403 Forbidden
2. Missing key -> 403 Forbidden
3. Cross-tenant sum: sum of all 100 tenant rollup counts equals global SQL total
"""
import sys, os, httpx, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def run_isolation_tests(profile: str = "1m"):
    print(f"=== Running Tenant Isolation Tests ({profile.upper()}) ===")
    cfg = get_config(profile)
    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_db_url"]
    api_url = "http://127.0.0.1:8000"
    client = httpx.Client(base_url=api_url)

    # 1. Missing header -> 403
    r = client.get("/tenants/1/orders/count?from=2026-10-01&to=2026-10-10")
    print(f"1. Missing X-Tenant-Key: {r.status_code}")
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # 2. Key for Tenant 1 on Tenant 2 path -> 403
    r = client.get("/tenants/2/orders/count?from=2026-10-01&to=2026-10-10", headers={"X-Tenant-Key": "tenant-key-1"})
    print(f"2. Tenant 1 key on Tenant 2 path: {r.status_code}")
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # 3. Arbitrary fake key -> 403
    r = client.get("/tenants/1/orders/count?from=2026-10-01&to=2026-10-10", headers={"X-Tenant-Key": "malicious-hacker-key"})
    print(f"3. Fake key: {r.status_code}")
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # 4. Valid key -> 200
    r = client.get("/tenants/1/orders/count?from=2026-10-01&to=2026-10-10", headers={"X-Tenant-Key": "tenant-key-1"})
    print(f"4. Valid key: {r.status_code}")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"

    # 5. Global partition sum check
    f_time = "2026-10-01 00:00:00"
    t_time = "2026-10-15 00:00:00"
    print(f"\n5. Verifying cross-tenant partition sum for range [{f_time} to {t_time})...")

    main_conn = psycopg2.connect(main_url)
    main_cur = main_conn.cursor()
    main_cur.execute("SELECT count(*) FROM orders WHERE created_at >= %s AND created_at < %s", (f_time, t_time))
    global_sql_count = main_cur.fetchone()[0]

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_cur = rollup_conn.cursor()
    rollup_cur.execute("""
        SELECT COALESCE(SUM(order_count), 0) FROM rollup_orders_hour 
        WHERE hour >= %s AND hour < %s
    """, (f_time, t_time))
    rollup_total_count = rollup_cur.fetchone()[0]

    print(f"  Global Main-DB count: {global_sql_count:,}")
    print(f"  Sum of Tenant rollups: {rollup_total_count:,}")
    assert global_sql_count == rollup_total_count, f"Mismatch: {global_sql_count} != {rollup_total_count}"

    main_cur.close()
    main_conn.close()
    rollup_cur.close()
    rollup_conn.close()
    client.close()

    print(">>> TENANT ISOLATION TESTS PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_isolation_tests(prof)
    sys.exit(0 if ok else 1)
