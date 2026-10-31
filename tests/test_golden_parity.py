#!/usr/bin/env python3
"""Golden parity verification:
Compares API endpoints (backed ONLY by rollup-db) against independent plain SQL
directly on main-db across 10 sample tenants and 3 ranges.
Every comparison must match with ZERO diff.
"""
import sys, httpx, psycopg2, yaml

def run_golden_check():
    print("=== Running Golden Correctness Suite (API vs Main-DB SQL) ===")
    
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    main_url = cfg["database"]["main_db_url"]
    excluded = tuple(cfg["business_rules"]["excluded_statuses"])
    admin_key = cfg["auth"]["admin_key"]
    base_url = "http://127.0.0.1:8000"

    main_conn = psycopg2.connect(main_url)
    main_cur = main_conn.cursor()
    client = httpx.Client(base_url=base_url, timeout=30.0)

    tenants = [1, 2, 3, 5, 10, 25, 50, 75, 90, 99]
    ranges = [
        ("2026-10-01 00:00:00", "2026-10-30 00:00:00", "30 days"),
        ("2026-10-23 00:00:00", "2026-10-30 00:00:00", "7 days"),
        ("2026-10-28 12:00:00", "2026-10-29 18:00:00", "30 hours"),
    ]

    total_checks = 0
    mismatches = 0

    for tid in tenants:
        headers = {"X-Tenant-Key": f"tenant-key-{tid}"}
        for f_time, t_time, r_label in ranges:
            # 1. Check Count & Revenue (FR-1)
            # SQL Ground Truth
            main_cur.execute("""
                SELECT 
                    count(*),
                    coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
                FROM orders
                WHERE tenant_id = %s AND created_at >= %s AND created_at < %s
            """, (excluded, tid, f_time, t_time))
            sql_cnt, sql_rev = main_cur.fetchone()
            sql_rev = float(sql_rev)

            # API Call
            res = client.get(f"/tenants/{tid}/orders/count?from={f_time}&to={t_time}", headers=headers)
            assert res.status_code == 200, f"API error: {res.text}"
            api_data = res.json()["data"]
            api_cnt = api_data["order_count"]
            api_rev = api_data["total_sales"]

            total_checks += 1
            if api_cnt != sql_cnt or abs(api_rev - sql_rev) > 0.01:
                print(f"[FAIL] Count mismatch: Tenant {tid} ({r_label}): SQL=({sql_cnt}, {sql_rev}) vs API=({api_cnt}, {api_rev})")
                mismatches += 1

            # 2. Check Breakdown by Status (FR-4)
            main_cur.execute("""
                SELECT status, count(*), coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0)
                FROM orders
                WHERE tenant_id = %s AND created_at >= %s AND created_at < %s
                GROUP BY status
            """, (excluded, tid, f_time, t_time))
            sql_status_map = {r[0]: (r[1], float(r[2])) for r in main_cur.fetchall()}

            res = client.get(f"/tenants/{tid}/orders/by-status?from={f_time}&to={t_time}", headers=headers)
            assert res.status_code == 200
            api_status_map = {r["status"]: (r["order_count"], float(r["sales"])) for r in res.json()["data"]}

            total_checks += 1
            for st, (sc, sr) in sql_status_map.items():
                ac, ar = api_status_map.get(st, (0, 0.0))
                if ac != sc or abs(ar - sr) > 0.01:
                    print(f"[FAIL] Status mismatch for {st} on Tenant {tid}: SQL=({sc}, {sr}) vs API=({ac}, {ar})")
                    mismatches += 1

            # 3. Check Top 10 Products by Sales (FR-3) (on daily snapped range)
            # Snapped to whole days for daily queries
            f_day = f_time[:10] + " 00:00:00"
            t_day = t_time[:10] + " 00:00:00"
            if t_time[11:] != "00:00:00":
                import datetime
                t_day = (datetime.datetime.fromisoformat(t_time[:10]) + datetime.timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")

            main_cur.execute("""
                SELECT oi.product_id, sum(oi.qty) AS units, sum(oi.qty * oi.price) AS sales
                FROM order_items oi
                JOIN orders o ON o.order_id = oi.order_id
                WHERE o.tenant_id = %s
                  AND o.created_at >= %s AND o.created_at < %s
                  AND o.status NOT IN %s
                GROUP BY oi.product_id
                ORDER BY sales DESC
                LIMIT 10
            """, (tid, f_day, t_day, excluded))
            sql_top = [(r[0], r[1], float(r[2])) for r in main_cur.fetchall()]

            res = client.get(f"/tenants/{tid}/products/top?from={f_day}&to={t_day}&n=10&by=sales", headers=headers)
            assert res.status_code == 200
            api_top = [(r["product_id"], r["units"], float(r["sales"])) for r in res.json()["data"]]

            total_checks += 1
            if len(sql_top) != len(api_top):
                print(f"[FAIL] Top products length mismatch on Tenant {tid}: SQL={len(sql_top)} vs API={len(api_top)}")
                mismatches += 1
            else:
                for (sp, su, ss), (ap, au, ass) in zip(sql_top, api_top):
                    if sp != ap or su != au or abs(ss - ass) > 0.01:
                        print(f"[FAIL] Top product item mismatch on Tenant {tid}: SQL=({sp}, {su}, {ss}) vs API=({ap}, {au}, {ass})")
                        mismatches += 1

    print(f"\n==================================================")
    print(f"GOLDEN PARITY RESULTS:")
    print(f"Total query validations: {total_checks}")
    print(f"Mismatches detected:     {mismatches}")
    print(f"Parity Rate:             {((total_checks - mismatches) / total_checks) * 100:.2f}%")
    print(f"==================================================")

    main_cur.close()
    main_conn.close()
    client.close()
    return mismatches == 0

if __name__ == "__main__":
    success = run_golden_check()
    sys.exit(0 if success else 1)
