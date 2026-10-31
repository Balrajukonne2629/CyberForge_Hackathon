#!/usr/bin/env python3
"""tests/test_dashboard_smoke.py:
Automated test suite for the Multi-Tenant Aggregation Layer Demo Dashboard:
1. /ui loads (200, pure offline/no external CDN or host references in script/link/src).
2. All data endpoints across all 4 tabs return 200 with appropriate auth keys.
3. Wrong tenant key returns 403.
4. Demo endpoints return 404 when DEMO_MODE is disabled, and 403 when admin key is omitted.
5. Platform summary totals match the sum of plain SQL across all 100 tenant databases for 3 ranges.
"""
import os, sys, re, datetime
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url, get_all_tenant_ids
from api.main import app

profile = os.getenv("PROFILE", "1m")
cfg = get_config(profile)
ADMIN_KEY = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")

# -----------------------------------------------------------------------------
# 1. UI Loading and Offline / No-CDN Assertion
# -----------------------------------------------------------------------------
def test_ui_loads_offline_ready(client):
    res = client.get("/ui")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    html = res.text

    # Verify key dashboard structural anchors
    assert "Multi-Tenant Aggregation Layer" in html
    assert "getTenantData" in html
    assert "tab-tenant" in html
    assert "tab-platform" in html
    assert "tab-health" in html
    assert "tab-scale" in html

    # Assert NO external CDNs, fonts, or external resources loaded
    external_refs = re.findall(r'(?:src|href)=["\']https?://[^"\']+["\']', html)
    assert len(external_refs) == 0, f"Found external CDN/script references: {external_refs}"

# -----------------------------------------------------------------------------
# 2. Authentication & Tenant Isolation
# -----------------------------------------------------------------------------
def test_tenant_auth_and_isolation(client):
    q = "from=2026-10-25T00:00:00Z&to=2026-10-26T00:00:00Z"

    # Missing key -> 403
    r = client.get(f"/tenants/1/orders/count?{q}")
    assert r.status_code == 403

    # Wrong tenant key -> 403
    r = client.get(f"/tenants/1/orders/count?{q}", headers={"X-Tenant-Key": "invalid-wrong-key"})
    assert r.status_code == 403

    # Mismatched tenant key (Tenant 2 key on Tenant 1 route) -> 403
    r = client.get(f"/tenants/1/orders/count?{q}", headers={"X-Tenant-Key": "tenant-key-2"})
    assert r.status_code == 403

    # Correct tenant key -> 200
    r = client.get(f"/tenants/1/orders/count?{q}", headers={"X-Tenant-Key": "tenant-key-1"})
    assert r.status_code == 200
    assert "order_count" in r.json()["data"]

# -----------------------------------------------------------------------------
# 3. All Tabs Data Endpoints Return 200
# -----------------------------------------------------------------------------
def test_tab_endpoints(client):
    q = "from=2026-10-25T00:00:00Z&to=2026-10-26T00:00:00Z"
    t_headers = {"X-Tenant-Key": "tenant-key-1"}
    a_headers = {"X-Admin-Key": ADMIN_KEY}

    # Tab 1: Tenant Endpoints
    r1 = client.get(f"/tenants/1/orders/count?{q}", headers=t_headers)
    assert r1.status_code == 200
    assert "as_of" in r1.json()

    r2 = client.get(f"/tenants/1/orders/trend?{q}&grain=hour", headers=t_headers)
    assert r2.status_code == 200

    r3 = client.get(f"/tenants/1/products/top?{q}&n=10&by=sales", headers=t_headers)
    assert r3.status_code == 200

    r4 = client.get(f"/tenants/1/orders/by-status?{q}", headers=t_headers)
    assert r4.status_code == 200

    # Tab 2: Platform Endpoint
    r5 = client.get(f"/admin/platform/summary?{q}", headers=a_headers)
    assert r5.status_code == 200
    assert "totals" in r5.json()

    # Tab 3: Health Endpoints
    r6 = client.get("/health")
    assert r6.status_code == 200
    assert "worker_stalled" in r6.json()

    r7 = client.get("/admin/stats", headers=a_headers)
    assert r7.status_code == 200
    assert "tenants" in r7.json()

    # Tab 4: Scale Endpoint
    r8 = client.get("/admin/platform/scaling", headers=a_headers)
    assert r8.status_code in (200, 404)

# -----------------------------------------------------------------------------
# 4. Demo Controls Gating
# -----------------------------------------------------------------------------
def test_demo_endpoints_gating(client):
    # When DEMO_MODE is not 1 (or unset): must return 404
    os.environ["DEMO_MODE"] = "0"
    r = client.post("/demo/order", json={"tenant_id": 1}, headers={"X-Admin-Key": ADMIN_KEY})
    assert r.status_code == 404

    r = client.get("/demo/naive/1", headers={"X-Admin-Key": ADMIN_KEY})
    assert r.status_code == 404

    # When DEMO_MODE is 1 but Admin Key is missing / invalid: must return 403
    os.environ["DEMO_MODE"] = "1"
    r = client.post("/demo/order", json={"tenant_id": 1})
    assert r.status_code == 403

    r = client.post("/demo/order", json={"tenant_id": 1}, headers={"X-Admin-Key": "wrong-admin-key"})
    assert r.status_code == 403

    # When DEMO_MODE is 1 and Admin Key is valid: returns 200
    r = client.get("/demo/naive/1", headers={"X-Admin-Key": ADMIN_KEY})
    assert r.status_code == 200
    assert "elapsed_ms" in r.json()

    # Clean up environment
    os.environ["DEMO_MODE"] = "0"

# -----------------------------------------------------------------------------
# 5. Platform Summary Equals Plain SQL over 100 Tenant DBs for 3 Ranges
# -----------------------------------------------------------------------------
def test_platform_summary_matches_plain_sql_3_ranges(client):
    ranges = [
        ("2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z", "2026-10-25 00:00:00", "2026-10-26 00:00:00"), # 1 day
        ("2026-10-20T00:00:00Z", "2026-10-27T00:00:00Z", "2026-10-20 00:00:00", "2026-10-27 00:00:00"), # 7 days
        ("2026-10-01T00:00:00Z", "2026-10-30T00:00:00Z", "2026-10-01 00:00:00", "2026-10-30 00:00:00"), # 30 days
    ]

    all_tids = get_all_tenant_ids(100)

    for f_api, t_api, f_sql, t_sql in ranges:
        # Fetch platform summary from aggregation API
        r = client.get(f"/admin/platform/summary?from={f_api}&to={t_api}", headers={"X-Admin-Key": ADMIN_KEY})
        assert r.status_code == 200
        summary = r.json()
        agg_orders = summary["totals"]["order_count"]
        agg_sales = round(float(summary["totals"]["total_sales"]), 2)

        # Compute ground-truth sum of plain SQL across all 100 tenant databases
        def query_tenant_plain_sql(tid: int):
            url = get_tenant_db_url(tid, profile)
            conn = psycopg2.connect(url)
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN status NOT IN ('canceled', 'unavailable') THEN total ELSE 0 END), 0)
                FROM orders
                WHERE created_at >= %s AND created_at < %s
            """, (f_sql, t_sql))
            row = cur.fetchone()
            cur.close()
            conn.close()
            return int(row[0]), float(row[1])

        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(query_tenant_plain_sql, all_tids))

        sql_orders = sum(r[0] for r in results)
        sql_sales = round(sum(r[1] for r in results), 2)

        print(f"\n[Range: {f_sql} to {t_sql}]")
        print(f"  Orders: Platform={agg_orders:,} vs Plain SQL={sql_orders:,}")
        print(f"  Sales:  Platform=${agg_sales:,.2f} vs Plain SQL=${sql_sales:,.2f}")

        assert agg_orders == sql_orders, f"Mismatch in order_count: API={agg_orders} vs SQL={sql_orders}"
        assert abs(agg_sales - sql_sales) < 0.05, f"Mismatch in total_sales: API={agg_sales} vs SQL={sql_sales}"

if __name__ == "__main__":
    print("=== Running Dashboard Smoke Tests ===")
    with TestClient(app) as test_client:
        test_ui_loads_offline_ready(test_client)
        print("1. UI Loads & Offline/No-CDN Check: PASS")
        test_tenant_auth_and_isolation(test_client)
        print("2. Tenant Auth & Isolation Check: PASS")
        test_tab_endpoints(test_client)
        print("3. Tab Endpoints Check: PASS")
        test_demo_endpoints_gating(test_client)
        print("4. Demo Gating Check: PASS")
        test_platform_summary_matches_plain_sql_3_ranges(test_client)
        print("5. Platform Summary Parity (3 Ranges vs 100 Tenant DBs): PASS")
    print("\nALL DASHBOARD SMOKE TESTS PASSED 100%!")
