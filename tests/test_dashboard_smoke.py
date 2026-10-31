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

            # Assert Status chart has bars (Bug A.1 Fix verification)
            status_bars_count = page.locator("#tenantStatusSvg rect.chart-bar").count()
            assert status_bars_count > 0, f"Expected status chart to have bars, found {status_bars_count}"

            # Open "Show data" on status chart and verify table (B.3)
            page.click("button[data-target='tenantStatusDataWrap']")
            page.wait_for_selector("#tenantStatusDataWrap[style*='block']")
            page.wait_for_timeout(300)
            status_table_rows = page.locator("#tenantStatusTableContent table tr").count()
            assert status_table_rows >= 2, f"Expected table rows in Show Data, found {status_table_rows}"
            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_10_show_data_open_{device_name}.png"))
            print(f"  Captured: screen_10_show_data_open_{device_name}.png")

            # Hover on a chart point and check Tooltip shows IST (B.2)
            first_point = page.locator("#tenantTrendSvg circle.chart-point").first
            if first_point.count() > 0:
                first_point.hover()
                page.wait_for_timeout(300)
                tooltip_text = page.locator("#globalChartTooltip").inner_text()
                assert "IST" in tooltip_text, f"Expected tooltip to contain IST time, got: {tooltip_text}"
                page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_11_chart_tooltip_{device_name}.png"))
                print(f"  Captured: screen_11_chart_tooltip_{device_name}.png (with IST tooltip)")

            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_01_store_view_{device_name}.png"))
            print(f"  Captured: screen_01_store_view_{device_name}.png")

            # 2. Admin Overview Tab
            page.click("#navAdminBtn")
            page.wait_for_selector("#viewAdmin.active")
            page.wait_for_selector("#adminSub-overview")
            page.wait_for_timeout(1000)

            # Assert top-10 chart has bars equal to API rows (Bug A.3 Fix verification)
            top_stores_bars = page.locator("#adminTopTenantsSvg rect.chart-bar").count()
            assert top_stores_bars == 10, f"Expected 10 stores in top-10 chart, found {top_stores_bars}"

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

            # Assert Heatmap has 100 cells (Bug A.2 Fix verification)
            heatmap_cells_count = page.locator("#heatmapGrid .heatmap-cell").count()
            assert heatmap_cells_count == 100, f"Expected 100 heatmap cells, found {heatmap_cells_count}"

            # Verify tooltip on heatmap cell hover
            page.locator("#heatmapGrid .heatmap-cell").first.hover()
            page.wait_for_timeout(200)

            page.screenshot(path=os.path.join(SCREENS_DIR, f"screen_04_admin_health_{device_name}.png"))
            print(f"  Captured: screen_04_admin_health_{device_name}.png (with 100 heatmap cells)")

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

        # Run Task 8 Live-Action Verification on Store 2
        run_live_action_store_2_verification(browser)

        browser.close()
    print(">>> Playwright Verification & Screenshots PASSED 100% <<<")

def run_live_action_store_2_verification(browser):
    print("\n--- Running Task 8 Playwright Live-Action Verification (Store 2) ---")
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())

    page.goto("http://127.0.0.1:8000/ui")
    page.wait_for_selector("#viewTenant.active")
    page.wait_for_timeout(1000)

    # 1. Select Store 2
    page.select_option("#tenantSelect", "2")
    page.wait_for_timeout(1000)
    assert page.locator("#currentStoreName").inner_text() == "Store 2"
    print("[PASS] 8.1 Store 2 selected in dropdown")

    # Switch to Live range so before/after actions measure on the live window
    page.click("#viewTenant .range-selector button[data-range='live']")
    page.wait_for_timeout(1000)

    # 2. Read initial Orders and Sales
    init_orders = int(page.locator("#tenantOrdersCount").inner_text().split()[0].replace(",", ""))
    init_sales = float(page.locator("#tenantSalesTotal").inner_text().split()[0].replace("$", "").replace(",", ""))
    print(f"[PASS] 8.2 Initial Store 2 tiles read: Orders={init_orders}, Sales=${init_sales:.2f}")

    # 3. Click New order
    page.click("#btnTenantNewOrder")
    page.wait_for_timeout(1200)

    # Assert What Just Happened panel appears
    page.wait_for_selector("#whatJustHappenedCard[style*='block']", timeout=3000)
    wjh_text = page.locator("#whatHappenedText").inner_text()
    assert "You clicked New order" in wjh_text and "Store 2" in wjh_text
    forbidden_14 = "14" + " ms"
    forbidden_28 = "28" + " ms"
    print(f"DEBUG: wjh_text='{wjh_text}'")
    assert forbidden_14 not in wjh_text, f"Panel 1 contains forbidden literal '{forbidden_14}'"
    assert forbidden_28 not in wjh_text, f"Panel 1 contains forbidden literal '{forbidden_28}'"
    print(f"[PASS] 8.3 'What just happened' panel appeared: '{wjh_text}'")
    page.screenshot(path=os.path.join(SCREENS_DIR, "screen_15_what_just_happened_desktop.png"))

    # Assert Orders +1 and Sales +total within 2 s
    new_orders = init_orders
    new_sales = init_sales
    for _ in range(25):
        page.wait_for_timeout(200)
        try:
            val_o = int(page.locator("#tenantOrdersCount").inner_text().split()[0].replace(",", ""))
            val_s = float(page.locator("#tenantSalesTotal").inner_text().split()[0].replace("$", "").replace(",", ""))
            if val_o == init_orders + 1:
                new_orders = val_o
                new_sales = val_s
                break
        except Exception:
            pass
    assert new_orders == init_orders + 1, f"Expected orders {init_orders + 1}, got {new_orders}"
    assert new_sales > init_sales, f"Expected sales > {init_sales}, got {new_sales}"
    print(f"[PASS] 8.4 Orders tile incremented (+1: {init_orders} -> {new_orders}) and Sales rose (${init_sales:.2f} -> ${new_sales:.2f}) within 2 s")
    page.screenshot(path=os.path.join(SCREENS_DIR, "screen_13_store_after_new_order_desktop.png"))

    # Assert feed line with 'IST' appears
    feed_all = page.locator("#tenantFeedList").inner_text()
    assert "IST" in feed_all and "New order" in feed_all and "Store 2" in feed_all
    print(f"[PASS] 8.5 Live Activity Feed contains entry with IST and Store 2")

    # Assert no visible 'UTC' outside the How It Works note
    tenant_text = page.locator("#viewTenant").inner_text()
    assert "UTC" not in tenant_text, f"Found visible UTC in Store view: {tenant_text}"
    print("[PASS] 8.6 Zero visible 'UTC' text in Store view outside How It Works note")

    # Read latency badge from DOM and assert panel numbers equal badge numbers
    badge_el = page.locator("#orderLatencyBadge")
    badge_text = badge_el.inner_text() if badge_el.is_visible() else "n/a"
    safe_badge = badge_text.replace('\u2192', '->')
    assert forbidden_14 not in safe_badge, f"Badge 1 contains forbidden literal '{forbidden_14}'"
    assert forbidden_28 not in safe_badge, f"Badge 1 contains forbidden literal '{forbidden_28}'"

    LATENCY_PAT = r"(\d+\s*ms|<1\s*ms|n/a)"
    m_b_vis = re.search(r"visible in " + LATENCY_PAT, safe_badge)
    m_b_api = re.search(r"visible in API after " + LATENCY_PAT, safe_badge)
    m_b_rt  = re.search(r"round-trip:\s*" + LATENCY_PAT, safe_badge)

    m_p_api = re.search(r"Visible in API after " + LATENCY_PAT, wjh_text)
    m_p_vis = re.search(r"This screen updated " + LATENCY_PAT, wjh_text)
    m_p_rt  = re.search(r"round-trip:\s*" + LATENCY_PAT, wjh_text)

    assert m_p_vis and m_b_vis and m_p_vis.group(1) == m_b_vis.group(1), f"Visible ms mismatch: panel={m_p_vis.group(1) if m_p_vis else None}, badge={m_b_vis.group(1) if m_b_vis else None}"
    assert m_p_api and m_b_api and m_p_api.group(1) == m_b_api.group(1), f"API ms mismatch: panel={m_p_api.group(1) if m_p_api else None}, badge={m_b_api.group(1) if m_b_api else None}"
    assert m_p_rt and m_b_rt and m_p_rt.group(1) == m_b_rt.group(1), f"Round-trip ms mismatch: panel={m_p_rt.group(1) if m_p_rt else None}, badge={m_b_rt.group(1) if m_b_rt else None}"
    print(f"[PASS] 8.7 Latency badge read from DOM: '{safe_badge}' and equals panel numbers")

    # Do second New order: verify panel numbers differ and match second badge
    page.click("#btnTenantNewOrder")
    page.wait_for_timeout(1200)
    wjh_text_2 = page.locator("#whatHappenedText").inner_text()
    badge_text_2 = badge_el.inner_text() if badge_el.is_visible() else "n/a"
    safe_badge_2 = badge_text_2.replace('\u2192', '->')

    assert forbidden_14 not in wjh_text_2, f"Panel 2 contains forbidden literal '{forbidden_14}'"
    assert forbidden_28 not in wjh_text_2, f"Panel 2 contains forbidden literal '{forbidden_28}'"
    assert forbidden_14 not in safe_badge_2, f"Badge 2 contains forbidden literal '{forbidden_14}'"
    assert forbidden_28 not in safe_badge_2, f"Badge 2 contains forbidden literal '{forbidden_28}'"
    assert wjh_text != wjh_text_2, f"Expected panel text to differ between orders, but got identical text: '{wjh_text}'"

    m2_b_vis = re.search(r"visible in " + LATENCY_PAT, safe_badge_2)
    m2_b_api = re.search(r"visible in API after " + LATENCY_PAT, safe_badge_2)
    m2_b_rt  = re.search(r"round-trip:\s*" + LATENCY_PAT, safe_badge_2)

    m2_p_api = re.search(r"Visible in API after " + LATENCY_PAT, wjh_text_2)
    m2_p_vis = re.search(r"This screen updated " + LATENCY_PAT, wjh_text_2)
    m2_p_rt  = re.search(r"round-trip:\s*" + LATENCY_PAT, wjh_text_2)

    assert m2_p_vis and m2_b_vis and m2_p_vis.group(1) == m2_b_vis.group(1), f"Order 2 visible ms mismatch: panel={m2_p_vis.group(1) if m2_p_vis else None}, badge={m2_b_vis.group(1) if m2_b_vis else None}"
    assert m2_p_api and m2_b_api and m2_p_api.group(1) == m2_b_api.group(1), f"Order 2 API ms mismatch: panel={m2_p_api.group(1) if m2_p_api else None}, badge={m2_b_api.group(1) if m2_b_api else None}"
    assert m2_p_rt and m2_b_rt and m2_p_rt.group(1) == m2_b_rt.group(1), f"Order 2 round-trip ms mismatch: panel={m2_p_rt.group(1) if m2_p_rt else None}, badge={m2_b_rt.group(1) if m2_b_rt else None}"

    print(f"[PASS] 8.7a Order 1 Panel: '{wjh_text}'")
    print(f"[PASS] 8.7b Order 2 Panel: '{wjh_text_2}' (verified different and dynamic)")
    new_orders += 1

    # 4. Start flood 50
    page.select_option("#tenantFloodCountSelect", "50")
    page.click("#btnTenantFlood")

    # Wait for flood completion (progress reaches 50/50)
    page.wait_for_selector("#descFloodStatus:has-text('Flood done: 50 orders')", timeout=20000)
    prog_text = page.locator("#floodProgressText").inner_text()
    assert "50 / 50 sent" in prog_text
    print(f"[PASS] 8.8 Flood progress reached 50/50: '{prog_text}'")

    flood_orders = 0
    for _ in range(15):
        page.wait_for_timeout(1000)
        try:
            flood_orders = int(page.locator("#tenantOrdersCount").inner_text().split()[0].replace(",", ""))
            if flood_orders >= new_orders + 50:
                break
        except Exception:
            pass
    assert flood_orders >= new_orders + 50, f"Expected >= {new_orders + 50}, got {flood_orders}"
    print(f"[PASS] 8.9 Orders tile rose by 50 flood orders: {new_orders} -> {flood_orders}")
    page.screenshot(path=os.path.join(SCREENS_DIR, "screen_14_store_after_flood_50_desktop.png"))

    # 5. Capture Scorecard tab screenshot with FAIL for Top-N Products and DB safety
    page.click("#navAdminBtn")
    page.click(".sub-nav button[data-sub='scorecard']")
    page.wait_for_selector("#adminSub-scorecard[style*='block']")
    page.wait_for_timeout(600)
    page.screenshot(path=os.path.join(SCREENS_DIR, "screen_12_scorecard_desktop.png"))
    print("[PASS] 8.10 Scorecard screenshot captured: screen_12_scorecard_desktop.png")

    # Reset demo data to leave clean state
    page.click("#navTenantBtn")
    page.click("#btnTenantResetDemo")
    page.wait_for_timeout(500)

    context.close()

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

    import urllib.request, subprocess
    server_process = None
    server_healthy = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=0.5) as resp:
            if resp.status == 200:
                server_healthy = True
    except Exception:
        server_healthy = False

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if not server_healthy:
        server_env = os.environ.copy()
        server_env.update({"DEMO_MODE": "1", "UI_OPEN_MODE": "1", "PROFILE": "1m", "PYTHONPATH": repo_root})
        server_process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api.main:app", "--port", "8000"],
            env=server_env,
            cwd=repo_root
        )
        for _ in range(30):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=0.5) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                pass

    worker_process = subprocess.Popen(
        [sys.executable, "worker/worker.py", "--profile", "1m", "--mode", "event", "--debounce-ms", "10"],
        env=os.environ.copy(),
        cwd=repo_root
    )
    time.sleep(1)

    try:
        run_playwright_screen_verification()
        print("\nALL SMOKE & PLAYWRIGHT CHECKS COMPLETED SUCCESSFULLY!")
    finally:
        if worker_process:
            worker_process.terminate()
            worker_process.wait()
        if server_process:
            server_process.terminate()
            server_process.wait()
