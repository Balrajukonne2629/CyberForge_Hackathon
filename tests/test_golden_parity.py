#!/usr/bin/env python3
"""tests/test_golden_parity.py:
Comprehensive golden comparison across ALL 4 endpoints, hourly and daily trend grains,
10 sample tenants, 5 standard ranges, and 3 edge ranges against independent plain SQL
executed directly on each tenant's OWN separate database.
"""
import sys, os, datetime, httpx, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url

def run_golden_check(profile: str = "1m") -> bool:
    cfg = get_config(profile)
    excluded = tuple(cfg["business_rules"]["excluded_statuses"])
    base_url = "http://127.0.0.1:8000"

    print("=" * 70)
    print(f"RUNNING GOLDEN PARITY CHECK (PROFILE: {profile.upper()})")
    print("Testing against per-tenant isolated databases.")
    print("=" * 70)

    client = httpx.Client(base_url=base_url, timeout=30.0)

    # 10 tenants: biggest (1), smallest (99), random (42, 77), plus representative spread
    tenants = [1, 99, 42, 77, 2, 5, 10, 25, 50, 90]

    # Current hour for edge range
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cur_hour_start = (now_utc - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:00:00")
    cur_hour_end = now_utc.strftime("%Y-%m-%d %H:00:00")

    ranges = [
        # Standard 5 ranges
        ("2026-10-25 10:00:00", "2026-10-25 11:00:00", "1 hour"),
        ("2026-10-25 00:00:00", "2026-10-26 00:00:00", "1 day"),
        ("2026-10-23 00:00:00", "2026-10-30 00:00:00", "7 days"),
        ("2026-10-01 00:00:00", "2026-10-30 00:00:00", "30 days"),
        ("2024-10-31 00:00:00", "2026-10-30 00:00:00", "full span"),
        # Edge ranges
        ("2020-01-01 00:00:00", "2020-01-02 00:00:00", "empty range"),
        (cur_hour_start, cur_hour_end, "current hour"),
        ("2026-09-25 00:00:00", "2026-10-05 00:00:00", "month boundary"),
    ]

    total_checks = 0
    mismatches = 0

    for tid in tenants:
        t_url = get_tenant_db_url(tid, profile)
        t_conn = psycopg2.connect(t_url)
        t_cur = t_conn.cursor()
        headers = {"X-Tenant-Key": f"tenant-key-{tid}"}

        for f_time, t_time, r_label in ranges:
            # 1. /orders/count
            t_cur.execute("""
                SELECT 
                    count(*),
                    coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
                FROM orders
                WHERE tenant_id = %s AND created_at >= %s AND created_at < %s
            """, (excluded, tid, f_time, t_time))
            sql_cnt, sql_rev = t_cur.fetchone()
            sql_rev = float(sql_rev)

            res = client.get(f"/tenants/{tid}/orders/count?from={f_time}&to={t_time}", headers=headers)
            assert res.status_code == 200, f"Error: {res.text}"
            data = res.json()["data"]
            total_checks += 1
            if data["order_count"] != sql_cnt or abs(data["total_sales"] - sql_rev) > 0.01:
                print(f"[FAIL] Count mismatch: T{tid} ({r_label}): SQL=({sql_cnt},{sql_rev}) vs API=({data['order_count']},{data['total_sales']})")
                mismatches += 1

            # 2. /orders/trend (grain=hour)
            t_cur.execute("""
                SELECT date_trunc('hour', created_at) AS hr, count(*),
                       coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
                FROM orders
                WHERE tenant_id = %s AND created_at >= %s AND created_at < %s
                GROUP BY 1 ORDER BY 1
            """, (excluded, tid, f_time, t_time))
            sql_hr_trend = {r[0].strftime("%Y-%m-%dT%H:00:00Z"): (r[1], float(r[2])) for r in t_cur.fetchall()}

            res = client.get(f"/tenants/{tid}/orders/trend?from={f_time}&to={t_time}&grain=hour", headers=headers)
            assert res.status_code == 200
            api_hr_trend = {r["bucket"]: (r["order_count"], float(r["sales"])) for r in res.json()["data"]}

            total_checks += 1
            if len(sql_hr_trend) != len(api_hr_trend):
                print(f"[FAIL] Hourly trend length mismatch: T{tid} ({r_label})")
                mismatches += 1
            else:
                for b, (sc, ss) in sql_hr_trend.items():
                    ac, as_val = api_hr_trend.get(b, (0, 0.0))
                    if ac != sc or abs(as_val - ss) > 0.01:
                        print(f"[FAIL] Hourly trend value mismatch: T{tid} bucket {b}")
                        mismatches += 1

            # 3. /orders/trend (grain=day) (snapped to whole day)
            f_day = f_time[:10] + " 00:00:00"
            t_day = t_time[:10] + " 00:00:00"
            if t_time[11:] != "00:00:00":
                t_day = (datetime.datetime.fromisoformat(t_time[:10]) + datetime.timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")

            t_cur.execute("""
                SELECT date_trunc('day', created_at) AS dy, count(*),
                       coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
                FROM orders
                WHERE tenant_id = %s AND created_at >= %s AND created_at < %s
                GROUP BY 1 ORDER BY 1
            """, (excluded, tid, f_day, t_day))
            sql_day_trend = {r[0].strftime("%Y-%m-%dT00:00:00Z"): (r[1], float(r[2])) for r in t_cur.fetchall()}

            res = client.get(f"/tenants/{tid}/orders/trend?from={f_time}&to={t_time}&grain=day", headers=headers)
            assert res.status_code == 200
            api_day_trend = {r["bucket"]: (r["order_count"], float(r["sales"])) for r in res.json()["data"]}

            total_checks += 1
            if len(sql_day_trend) != len(api_day_trend):
                print(f"[FAIL] Daily trend length mismatch: T{tid} ({r_label})")
                mismatches += 1
            else:
                for b, (sc, ss) in sql_day_trend.items():
                    ac, as_val = api_day_trend.get(b, (0, 0.0))
                    if ac != sc or abs(as_val - ss) > 0.01:
                        print(f"[FAIL] Daily trend value mismatch: T{tid} bucket {b}")
                        mismatches += 1

            # 4. /orders/by-status
            t_cur.execute("""
                SELECT status, count(*),
                       coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
                FROM orders
                WHERE tenant_id = %s AND created_at >= %s AND created_at < %s
                GROUP BY status ORDER BY count(*) DESC
            """, (excluded, tid, f_time, t_time))
            sql_by_status = {r[0]: (r[1], float(r[2])) for r in t_cur.fetchall()}

            res = client.get(f"/tenants/{tid}/orders/by-status?from={f_time}&to={t_time}", headers=headers)
            assert res.status_code == 200
            api_by_status = {r["status"]: (r["order_count"], float(r["sales"])) for r in res.json()["data"]}

            total_checks += 1
            if len(sql_by_status) != len(api_by_status):
                print(f"[FAIL] By-status length mismatch: T{tid} ({r_label})")
                mismatches += 1
            else:
                for st, (sc, ss) in sql_by_status.items():
                    ac, as_val = api_by_status.get(st, (0, 0.0))
                    if ac != sc or abs(as_val - ss) > 0.01:
                        print(f"[FAIL] By-status mismatch: T{tid} status {st}")
                        mismatches += 1

            # 5. /products/top (by sales)
            t_cur.execute("""
                SELECT oi.product_id, coalesce(sum(oi.qty * oi.price), 0)
                FROM order_items oi
                JOIN orders o ON o.order_id = oi.order_id
                WHERE oi.tenant_id = %s AND o.status NOT IN %s AND o.created_at >= %s AND o.created_at < %s
                GROUP BY oi.product_id ORDER BY 2 DESC LIMIT 10
            """, (tid, excluded, f_day, t_day))
            sql_top_sales = [(r[0], float(r[1])) for r in t_cur.fetchall()]

            res = client.get(f"/tenants/{tid}/products/top?from={f_time}&to={t_time}&n=10&by=sales", headers=headers)
            assert res.status_code == 200
            api_top_sales = [(r["product_id"], float(r["sales"])) for r in res.json()["data"]]

            total_checks += 1
            if len(sql_top_sales) != len(api_top_sales):
                print(f"[FAIL] Top products sales count mismatch: T{tid} ({r_label})")
                mismatches += 1
            else:
                for i in range(len(sql_top_sales)):
                    if abs(sql_top_sales[i][1] - api_top_sales[i][1]) > 0.01:
                        print(f"[FAIL] Top sales val mismatch: T{tid} rank {i}")
                        mismatches += 1

            # 6. /products/top (by units)
            t_cur.execute("""
                SELECT oi.product_id, coalesce(sum(oi.qty), 0)
                FROM order_items oi
                JOIN orders o ON o.order_id = oi.order_id
                WHERE oi.tenant_id = %s AND o.status NOT IN %s AND o.created_at >= %s AND o.created_at < %s
                GROUP BY oi.product_id ORDER BY 2 DESC LIMIT 10
            """, (tid, excluded, f_day, t_day))
            sql_top_units = [(r[0], int(r[1])) for r in t_cur.fetchall()]

            res = client.get(f"/tenants/{tid}/products/top?from={f_time}&to={t_time}&n=10&by=units", headers=headers)
            assert res.status_code == 200
            api_top_units = [(r["product_id"], int(r["units"])) for r in res.json()["data"]]

            total_checks += 1
            if len(sql_top_units) != len(api_top_units):
                print(f"[FAIL] Top products units count mismatch: T{tid} ({r_label})")
                mismatches += 1
            else:
                for i in range(len(sql_top_units)):
                    if sql_top_units[i][1] != api_top_units[i][1]:
                        print(f"[FAIL] Top units val mismatch: T{tid} rank {i}")
                        mismatches += 1

        t_cur.close()
        t_conn.close()

    print(f"\nGOLDEN CHECK RESULTS: {total_checks - mismatches}/{total_checks} passed. Total mismatches: {mismatches}")
    return mismatches == 0

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_golden_check(prof)
    sys.exit(0 if ok else 1)
