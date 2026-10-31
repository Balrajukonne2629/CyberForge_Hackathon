#!/usr/bin/env python3
"""tests/test_dashboard_smoke.py:
Comprehensive smoke test suite for Multi-Tenant Aggregation Dashboard:
1. /ui loads with zero external hosts and NO key input fields.
2. Each view's /ui-api/ endpoints return 200 OK.
3. Real API (/tenants/ and /admin/) returns 403 Forbidden without valid credentials.
4. /ui-api/ returns 404 Not Found when UI_OPEN_MODE=0.
5. Demo endpoints return 404 Not Found when DEMO_MODE=0.
6. Platform summary equals the ground-truth sum of plain SQL across 100 tenant DBs for 3 ranges.
7. Isolation check triggers and verifies 403 boundary.
8. API Calls Panel logs calls, highlights 403/200, and Playwright captures all views (1920x1080 & 390px).
"""
import os, sys, re, time, datetime
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
import psycopg2
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url, get_all_tenant_ids
from api.main import app

profile = os.getenv("PROFILE", "1m")
cfg = get_config(profile)
ADMIN_KEY = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")
SCREENS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docs", "screens"))
os.makedirs(SCREENS_DIR, exist_ok=True)

# -----------------------------------------------------------------------------
# 1. /ui Loads with Zero External Hosts & NO Key Fields
# -----------------------------------------------------------------------------
def test_ui_loads_no_external_hosts_and_no_key_fields(client):
    res = client.get("/ui")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert "text/html" in res.headers["content-type"]
    html = res.text

    assert "Multi-Tenant Aggregation Layer" in html
    assert "apiInspector" in html
    assert "getTenantData" in html
    assert "viewTenant" in html
    assert "viewAdmin" in html
    assert "viewHowItWorks" in html

    # Assert NO external CDNs, fonts, or external scripts/links
    external_refs = re.findall(r'(?:src|href)=["\']https?://[^"\']+["\']', html)
    assert len(external_refs) == 0, f"Found external references: {external_refs}"

    # Assert NO key input fields or "set key" buttons in the UI
    key_inputs = re.findall(r'id=["\']\w*key\w*input["\']', html, re.I)
    assert len(key_inputs) == 0, f"Found key input fields: {key_inputs}"
    assert "adminKeyInput" not in html
    assert "tenantKeyInput" not in html
    assert "saveAdminKeyBtn" not in html
    assert "saveTenantKeyBtn" not in html
    assert "set key" not in html.lower()

# -----------------------------------------------------------------------------
# 2. Each View Endpoints Return 200 via /ui-api/
# -----------------------------------------------------------------------------
def test_each_view_endpoints_return_200(client):
    q = "from=2026-10-25T00:00:00Z&to=2026-10-26T00:00:00Z"

    # Store View Endpoints
    r = client.get("/ui-api/tenants")
    assert r.status_code == 200
    assert len(r.json()) >= 100

    r = client.get(f"/ui-api/tenants/1/orders/count?{q}")
    assert r.status_code == 200
    assert "data" in r.json()

    r = client.get(f"/ui-api/tenants/1/orders/trend?{q}&grain=hour")
    assert r.status_code == 200

    r = client.get(f"/ui-api/tenants/1/products/top?{q}&n=10&by=sales")
    assert r.status_code == 200

    r = client.get(f"/ui-api/tenants/1/orders/by-status?{q}")
    assert r.status_code == 200

    # Admin View Endpoints
    r = client.get(f"/ui-api/platform/summary?{q}")
    assert r.status_code == 200

    r = client.get(f"/ui-api/platform/tenants?{q}")
    assert r.status_code == 200

    r = client.get("/ui-api/monitor")
    assert r.status_code == 200

    r = client.get("/ui-api/stats")
    assert r.status_code == 200

# -----------------------------------------------------------------------------
# 3. Real API Returns 403 Without Key or With Mismatched Key
# -----------------------------------------------------------------------------
def test_real_api_returns_403_without_key(client):
    q = "from=2026-10-25T00:00:00Z&to=2026-10-26T00:00:00Z"

    # 1. Real tenant endpoint without key -> 403
    r = client.get(f"/tenants/1/orders/count?{q}")
    assert r.status_code == 403, f"Expected 403 for unauth tenant call, got {r.status_code}"

    # 2. Real tenant endpoint with wrong tenant key -> 403
    r = client.get(f"/tenants/1/orders/count?{q}", headers={"X-Tenant-Key": "tenant-key-2"})
    assert r.status_code == 403, f"Expected 403 for mismatched tenant key, got {r.status_code}"

    # 3. Real admin endpoints without admin key -> 403
    r = client.get(f"/admin/platform/summary?{q}")
    assert r.status_code == 403

    r = client.get("/admin/stats")
    assert r.status_code == 403

    r = client.get("/admin/monitor")
    assert r.status_code == 403

# -----------------------------------------------------------------------------
# 4. /ui-api Returns 404 When UI_OPEN_MODE=0
# -----------------------------------------------------------------------------
def test_ui_api_404_when_ui_open_mode_zero(client):
    orig = os.environ.get("UI_OPEN_MODE")
    try:
        os.environ["UI_OPEN_MODE"] = "0"
        r = client.get("/ui-api/tenants")
        assert r.status_code == 404, f"Expected 404 when UI_OPEN_MODE=0, got {r.status_code}"

        r = client.get("/ui-api/platform/summary?from=2026-10-25T00:00:00Z&to=2026-10-26T00:00:00Z")
        assert r.status_code == 404
    finally:
        if orig is not None:
            os.environ["UI_OPEN_MODE"] = orig
        else:
            os.environ["UI_OPEN_MODE"] = "1"

# -----------------------------------------------------------------------------
# 5. Demo Endpoints Return 404 When DEMO_MODE=0
# -----------------------------------------------------------------------------
def test_demo_endpoints_404_when_demo_mode_off(client):
    orig = os.environ.get("DEMO_MODE")
    try:
        os.environ["DEMO_MODE"] = "0"
        r = client.post("/demo/order", json={"tenant_id": 1}, headers={"X-Admin-Key": ADMIN_KEY})
        assert r.status_code == 404, f"Expected 404 when DEMO_MODE=0, got {r.status_code}"

        os.environ["DEMO_MODE"] = "1"
        r = client.get("/demo/keys", headers={"X-Admin-Key": ADMIN_KEY})
        assert r.status_code == 200
    finally:
        if orig is not None:
            os.environ["DEMO_MODE"] = orig
        else:
            os.environ["DEMO_MODE"] = "1"

# -----------------------------------------------------------------------------
# 6. Platform Summary Matches Plain SQL Over 100 Tenant DBs for 3 Ranges
# -----------------------------------------------------------------------------
def test_platform_summary_matches_plain_sql_3_ranges(client):
    ranges = [
        ("2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z", "2026-10-25 00:00:00", "2026-10-26 00:00:00"), # 1 day
        ("2026-10-20T00:00:00Z", "2026-10-27T00:00:00Z", "2026-10-20 00:00:00", "2026-10-27 00:00:00"), # 7 days
        ("2026-10-01T00:00:00Z", "2026-10-30T00:00:00Z", "2026-10-01 00:00:00", "2026-10-30 00:00:00"), # 30 days
    ]

    all_tids = get_all_tenant_ids(100)

    for f_api, t_api, f_sql, t_sql in ranges:
        r = client.get(f"/admin/platform/summary?from={f_api}&to={t_api}", headers={"X-Admin-Key": ADMIN_KEY})
        assert r.status_code == 200
        summary = r.json()
        agg_orders = summary["totals"]["order_count"]
        agg_sales = round(float(summary["totals"]["total_sales"]), 2)

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

# -----------------------------------------------------------------------------
# 7. Playwright Screenshot & Inspector End-to-End Verification
# -----------------------------------------------------------------------------
def run_playwright_screen_verification():
    print("\n--- Running Playwright End-to-End & Inspector Verification ---")
    url = "http://127.0.0.1:8000/ui"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for device_name, viewport in [("desktop", {"width": 1920, "height": 1080}), ("mobile", {"width": 390, "height": 844})]:
            context = browser.new_context(viewport=viewport)
            page = context.new_page()

            # 1. Store View (Default Landing)
            page.goto(url)
            page.wait_for_selector("#viewTenant.active")
            page.wait_for_timeout(1000)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_01_store_view_{device_name}.png"))
            print(f"  Captured: screen_01_store_view_{device_name}.png")

            # 2. Admin Overview Tab
            page.click("#navAdminBtn")
            page.wait_for_selector("#viewAdmin.active")
            page.wait_for_selector("#adminSub-overview")
            page.wait_for_timeout(1000)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_02_admin_overview_{device_name}.png"))
            print(f"  Captured: screen_02_admin_overview_{device_name}.png")

            # 3. Admin Stores Table
            page.click(".sub-nav button[data-sub='tenants']")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_03_admin_stores_{device_name}.png"))
            print(f"  Captured: screen_03_admin_stores_{device_name}.png")

            # 4. Admin Health & 10x10 Heatmap
            page.click(".sub-nav button[data-sub='health']")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_04_admin_health_{device_name}.png"))
            print(f"  Captured: screen_04_admin_health_{device_name}.png")

            # 5. Admin Scale Benchmark
            page.click(".sub-nav button[data-sub='scale']")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_05_admin_scale_{device_name}.png"))
            print(f"  Captured: screen_05_admin_scale_{device_name}.png")

            # 6. Admin Demo Controls
            page.click(".sub-nav button[data-sub='demo']")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_06_admin_demo_{device_name}.png"))
            print(f"  Captured: screen_06_admin_demo_{device_name}.png")

            # 7. How It Works View
            page.click("#navHowBtn")
            page.wait_for_selector("#viewHowItWorks.active")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_07_how_it_works_{device_name}.png"))
            print(f"  Captured: screen_07_how_it_works_{device_name}.png")

            # 8. Trigger Isolation Check
            page.click("#btnIsolationCheck")
            page.wait_for_selector("#isolationModal[style*='block']")
            page.wait_for_timeout(1000)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_08_isolation_check_{device_name}.png"))
            print(f"  Captured: screen_08_isolation_check_{device_name}.png")

            # 9. Verify API Calls Inspector (Check that 200 and 403 rows exist)
            page.click("#inspectorTableBody tr >> text=403")
            page.wait_for_timeout(400)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_09_inspector_open_{device_name}.png"))
            print(f"  Captured: screen_09_inspector_open_{device_name}.png (with 403 & 200 rows)")

            # Assertions on API Inspector Table
            rows_count = page.locator("#inspectorTableBody tr").count()
            assert rows_count >= 2, f"Expected at least 2 inspector rows, got {rows_count}"
            has_403 = page.locator("#inspectorTableBody tr:has-text('403')").count() > 0
            has_200 = page.locator("#inspectorTableBody tr:has-text('200')").count() > 0
            assert has_403, "Inspector missing 403 row from isolation check"
            assert has_200, "Inspector missing 200 row from dashboard calls"

            context.close()

        browser.close()
    print(">>> Playwright Verification & Screenshots PASSED 100% <<<")

if __name__ == "__main__":
    os.environ["DEMO_MODE"] = "1"
    os.environ["UI_OPEN_MODE"] = "1"
    print("=" * 70)
    print("RUNNING DASHBOARD SMOKE & PLAYWRIGHT TEST SUITE")
    print("=" * 70)

    with TestClient(app) as test_client:
        test_ui_loads_no_external_hosts_and_no_key_fields(test_client)
        print("[PASS] 1. UI Loads with Zero External Hosts & NO Key Fields")

        test_each_view_endpoints_return_200(test_client)
        print("[PASS] 2. Each View Endpoints Return 200 via /ui-api/")

        test_real_api_returns_403_without_key(test_client)
        print("[PASS] 3. Real API Returns 403 Without Key or With Mismatched Key")

        test_ui_api_404_when_ui_open_mode_zero(test_client)
        print("[PASS] 4. /ui-api Returns 404 When UI_OPEN_MODE=0")

        test_demo_endpoints_404_when_demo_mode_off(test_client)
        print("[PASS] 5. Demo Endpoints Return 404 When DEMO_MODE=0")

        test_platform_summary_matches_plain_sql_3_ranges(test_client)
        print("[PASS] 6. Platform Summary Parity (3 Ranges vs 100 Tenant DBs)")

    run_playwright_screen_verification()
    print("\nALL SMOKE & PLAYWRIGHT CHECKS COMPLETED SUCCESSFULLY!")
