#!/usr/bin/env python3
"""Baseline benchmark: direct naive SQL queries on main-db (without rollup layer).
Measures 4 core queries across 10 sample tenants with 5 warm-up and 30 timed runs.
"""
import sys, time, platform, os, ctypes, yaml, psycopg2, numpy as np

def get_hardware_info():
    uname = platform.uname()
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    ram_gb = mem.ullTotalPhys / (1024 ** 3)
    return {
        "os": f"{uname.system} {uname.release} ({uname.version})",
        "processor": uname.processor or platform.machine(),
        "cores": os.cpu_count(),
        "ram_gb": f"{ram_gb:.1f} GB",
        "python": platform.python_version()
    }

def run_baseline(db_url: str, label: str):
    hw = get_hardware_info()
    print("=" * 70)
    print(f"BASELINE BENCHMARK: Direct Main DB ({label})")
    print(f"Hardware: {hw['processor']} | {hw['cores']} logical CPUs | {hw['ram_gb']} RAM")
    print(f"OS: {hw['os']} | Python {hw['python']}")
    print("=" * 70)

    # Load config
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    excluded_statuses = tuple(cfg["business_rules"]["excluded_statuses"])
    tenants = [1, 2, 3, 5, 10, 25, 50, 75, 90, 99]
    date_from = "2026-10-01 00:00:00"
    date_to = "2026-10-30 00:00:00"

    queries = {
        "1. Count & Revenue (FR-1)": """
            SELECT count(*), coalesce(sum(total), 0)
            FROM orders
            WHERE tenant_id = %s
              AND created_at >= %s AND created_at < %s
              AND status NOT IN %s
        """,
        "2a. Hourly Trend (FR-2)": """
            SELECT date_trunc('hour', created_at) AS hr, count(*), coalesce(sum(total), 0)
            FROM orders
            WHERE tenant_id = %s
              AND created_at >= %s AND created_at < %s
              AND status NOT IN %s
            GROUP BY 1
            ORDER BY 1
        """,
        "2b. Daily Trend (FR-2)": """
            SELECT date_trunc('day', created_at) AS dy, count(*), coalesce(sum(total), 0)
            FROM orders
            WHERE tenant_id = %s
              AND created_at >= %s AND created_at < %s
              AND status NOT IN %s
            GROUP BY 1
            ORDER BY 1
        """,
        "3. Top 10 Products by Sales (FR-3)": """
            SELECT oi.product_id, p.category, sum(oi.qty) AS units, sum(oi.qty * oi.price) AS sales
            FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            JOIN products p ON p.product_id = oi.product_id
            WHERE o.tenant_id = %s
              AND o.created_at >= %s AND o.created_at < %s
              AND o.status NOT IN %s
            GROUP BY oi.product_id, p.category
            ORDER BY sales DESC
            LIMIT 10
        """,
        "4. Breakdown by Status (FR-4)": """
            SELECT status, count(*), coalesce(sum(total), 0)
            FROM orders
            WHERE tenant_id = %s
              AND created_at >= %s AND created_at < %s
            GROUP BY status
            ORDER BY count(*) DESC
        """
    }

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    overall_results = {}

    for q_name, sql in queries.items():
        overall_results[q_name] = []
        print(f"\n--- Running: {q_name} ---")
        tenant_p95s = []

        for tid in tenants:
            if "%s" in sql and "status NOT IN %s" in sql:
                params = (tid, date_from, date_to, excluded_statuses)
            else:
                params = (tid, date_from, date_to)

            # Warm-up (5 runs)
            for _ in range(5):
                cur.execute(sql, params)
                cur.fetchall()

            # Timed runs (30 runs)
            durations = []
            for _ in range(30):
                t0 = time.perf_counter()
                cur.execute(sql, params)
                cur.fetchall()
                durations.append((time.perf_counter() - t0) * 1000)

            p50 = float(np.percentile(durations, 50))
            p95 = float(np.percentile(durations, 95))
            mx = float(np.max(durations))
            tenant_p95s.append(p95)
            overall_results[q_name].extend(durations)

            if tid in (1, 10, 99):
                print(f"  Tenant {tid:2d}: p50={p50:6.2f}ms | p95={p95:6.2f}ms | max={mx:6.2f}ms")

        all_d = overall_results[q_name]
        q_p50 = float(np.percentile(all_d, 50))
        q_p95 = float(np.percentile(all_d, 95))
        q_max = float(np.max(all_d))
        print(f"  => Aggregate across all 10 tenants (300 runs): p50={q_p50:6.2f}ms | p95={q_p95:6.2f}ms | max={q_max:6.2f}ms")

    cur.close()
    conn.close()
    print("\n" + "=" * 70)
    print(f"SUMMARY FOR {label}:")
    for q_name, d in overall_results.items():
        print(f"  {q_name:<34}: p50={np.percentile(d, 50):6.2f}ms | p95={np.percentile(d, 95):6.2f}ms | max={np.max(d):6.2f}ms")
    print("=" * 70)

if __name__ == "__main__":
    from config import get_config
    cfg = get_config("1m")
    url = sys.argv[1] if len(sys.argv) > 1 else cfg["database"]["main_db_url"]
    label = sys.argv[2] if len(sys.argv) > 2 else "1M Dataset"
    run_baseline(url, label)
