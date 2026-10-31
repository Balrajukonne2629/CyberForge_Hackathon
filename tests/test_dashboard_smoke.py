#!/usr/bin/env python3
"""tests/test_dashboard_smoke.py:
Smoke test suite for Dashboard:
1. /ui loads with zero external hosts in script/link/src.
2. Role screens render (Landing, Admin Overview, Tenants, Health, Scale, Demo, Tenant View).
3. Admin endpoints return 403 when presented with a tenant key.
4. /demo/keys returns 404 when DEMO_MODE is off.
5. Platform summary totals match the ground-truth sum of plain SQL across 100 tenant databases for 3 ranges.
6. The API Inspector records calls (Playwright check), and captures screenshots (1920x1080 & 390px) into docs/screens/.
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
# 1. /ui Loads with Zero External Hosts
# -----------------------------------------------------------------------------
def test_ui_loads_no_external_hosts(client):
    res = client.get("/ui")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert "text/html" in res.headers["content-type"]
    html = res.text

    assert "Multi-Tenant Aggregation Layer" in html
    assert "landingScreen" in html
    assert "apiInspector" in html
    assert "getTenantData" in html

    # Assert NO external CDNs, fonts, or external scripts/links
    external_refs = re.findall(r'(?:src|href)=["\']https?://[^"\']+["\']', html)
    assert len(external_refs) == 0, f"Found external references: {external_refs}"

# -----------------------------------------------------------------------------
# 2. Admin Endpoints Return 403 for Tenant Key
# -----------------------------------------------------------------------------
def test_admin_endpoints_return_403_for_tenant_key(client):
    tenant_headers = {"X-Admin-Key": "tenant-key-1"}
    q = "from=2026-10-25T00:00:00Z&to=2026-10-26T00:00:00Z"

    # 1. /admin/platform/summary
    r = client.get(f"/admin/platform/summary?{q}", headers=tenant_headers)
    assert r.status_code == 403, f"Expected 403 for summary, got {r.status_code}"

    # 2. /admin/platform/tenants
    r = client.get(f"/admin/platform/tenants?{q}", headers=tenant_headers)
    assert r.status_code == 403, f"Expected 403 for tenants list, got {r.status_code}"

    # 3. /admin/stats
    r = client.get("/admin/stats", headers=tenant_headers)
    assert r.status_code == 403, f"Expected 403 for stats, got {r.status_code}"

    # 4. /admin/monitor
    r = client.get("/admin/monitor", headers=tenant_headers)
    assert r.status_code == 403, f"Expected 403 for monitor, got {r.status_code}"

    # 5. /demo/keys
    r = client.get("/demo/keys", headers=tenant_headers)
    assert r.status_code == 403, f"Expected 403 for demo keys, got {r.status_code}"

# -----------------------------------------------------------------------------
# 3. /demo/keys is 404 when DEMO_MODE is Off
# -----------------------------------------------------------------------------
def test_demo_keys_404_when_demo_mode_off(client):
    orig = os.environ.get("DEMO_MODE")
    try:
        os.environ["DEMO_MODE"] = "0"
        r = client.get("/demo/keys", headers={"X-Admin-Key": ADMIN_KEY})
        assert r.status_code == 404, f"Expected 404 when DEMO_MODE=0, got {r.status_code}"

        os.environ["DEMO_MODE"] = "1"
        r = client.get("/demo/keys", headers={"X-Admin-Key": ADMIN_KEY})
        assert r.status_code == 200, f"Expected 200 when DEMO_MODE=1, got {r.status_code}"
        d = r.json()
        assert d["admin_key"] == ADMIN_KEY
        assert "1" in d["tenant_keys"]
    finally:
        if orig is not None:
            os.environ["DEMO_MODE"] = orig
        else:
            os.environ["DEMO_MODE"] = "1"

# -----------------------------------------------------------------------------
# 4. Platform Summary Equals Plain SQL Over 100 Tenant DBs for 3 Ranges
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
# 5. Playwright Screenshot & API Inspector Verification
# -----------------------------------------------------------------------------
def run_playwright_screen_verification():
    print("\n--- Running Playwright End-to-End & Inspector Verification ---")
    url = "http://127.0.0.1:8000/ui"

    with sync_playwright() as p:
        # Launch Chromium headless
        browser = p.chromium.launch(headless=True)

        for device_name, viewport in [("desktop", {"width": 1920, "height": 1080}), ("mobile", {"width": 390, "height": 844})]:
            context = browser.new_context(viewport=viewport)
            page = context.new_page()

            # 1. Landing Screen
            page.goto(url)
            page.wait_for_selector("#landingScreen.active")
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_01_landing_{device_name}.png"))
            print(f"  Captured: screen_01_landing_{device_name}.png")

            # 2. Log in as Admin using demo key button
            page.click("#useDemoKeyBtn")
            page.wait_for_selector("#appScreen.active")
            page.wait_for_selector("#tab-overview.active")
            page.wait_for_timeout(1000)

            # 2a. Admin Overview Screen
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_02_admin_overview_{device_name}.png"))
            print(f"  Captured: screen_02_admin_overview_{device_name}.png")

            # 3. Admin Tenants Tab
            page.click("button[data-tab='tenants']")
            page.wait_for_selector("#tab-tenants.active")
            page.wait_for_timeout(1000)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_03_admin_tenants_{device_name}.png"))
            print(f"  Captured: screen_03_admin_tenants_{device_name}.png")

            # 4. Admin Health Tab
            page.click("button[data-tab='health']")
            page.wait_for_selector("#tab-health.active")
            page.wait_for_timeout(1000)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_04_admin_health_{device_name}.png"))
            print(f"  Captured: screen_04_admin_health_{device_name}.png")

            # 5. Admin Scale Tab
            page.click("button[data-tab='scale']")
            page.wait_for_selector("#tab-scale.active")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_05_admin_scale_{device_name}.png"))
            print(f"  Captured: screen_05_admin_scale_{device_name}.png")

            # 6. Admin Demo Controls Tab
            page.click("button[data-tab='demo']")
            page.wait_for_selector("#tab-demo.active")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_06_admin_demo_{device_name}.png"))
            print(f"  Captured: screen_06_admin_demo_{device_name}.png")

            # 7. Single Tenant View Tab
            page.click("button[data-tab='tenant-view']")
            page.wait_for_selector("#tab-tenant-view.active")
            page.wait_for_timeout(1000)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_07_tenant_view_{device_name}.png"))
            print(f"  Captured: screen_07_tenant_view_{device_name}.png")

            # 8. API Inspector Check: trigger a 403 call and verify both 200 and 403 rows exist
            page.evaluate("""
                async () => {
                    // Trigger a 403 call with invalid key
                    try {
                        await inspectedFetch('/tenants/1/orders/count?from=2026-10-01&to=2026-10-02', {
                            headers: { 'X-Tenant-Key': 'invalid-bad-key' }
                        }, 'rollup-db');
                    } catch (e) {}
                }
            """)
            page.wait_for_timeout(500)

            # Ensure inspector table is populated
            rows_count = page.locator("#inspectorTableBody tr").count()
            assert rows_count >= 2, f"Expected at least 2 inspector rows, got {rows_count}"

            # Check that 403 row exists and 200 row exists
            has_403 = page.locator("#inspectorTableBody td:text('403')").count() > 0
            has_200 = page.locator("#inspectorTableBody td:text('200')").count() > 0
            assert has_403, "Inspector missing 403 row"
            assert has_200, "Inspector missing 200 row"

            # Take screenshot with inspector clearly visible and selected row
            page.click("#inspectorTableBody tr >> text=403")
            page.wait_for_timeout(300)
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_08_inspector_open_{device_name}.png"))
            print(f"  Captured: screen_08_inspector_open_{device_name}.png (with 403 & 200 rows)")

            context.close()

        browser.close()
    print(">>> Playwright Verification & Screenshots PASSED 100% <<<")

if __name__ == "__main__":
    os.environ["DEMO_MODE"] = "1"
    print("=" * 70)
    print("RUNNING DASHBOARD SMOKE & PLAYWRIGHT TEST SUITE")
    print("=" * 70)

    with TestClient(app) as test_client:
        test_ui_loads_no_external_hosts(test_client)
        print("[PASS] 1. UI Loads with Zero External Hosts")

        test_admin_endpoints_return_403_for_tenant_key(test_client)
        print("[PASS] 2. Admin Endpoints Return 403 for Tenant Key")

        test_demo_keys_404_when_demo_mode_off(test_client)
        print("[PASS] 3. /demo/keys is 404 when DEMO_MODE=0 and 200 when DEMO_MODE=1")

        test_platform_summary_matches_plain_sql_3_ranges(test_client)
        print("[PASS] 4. Platform Summary Parity (3 Ranges vs 100 Tenant DBs)")

    run_playwright_screen_verification()
    print("\nALL SMOKE & INSPECTOR CHECKS COMPLETED SUCCESSFULLY!")
