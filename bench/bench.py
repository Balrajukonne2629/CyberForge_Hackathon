#!/usr/bin/env python3
"""bench/bench.py:
Comprehensive Benchmark and Trust Harness (PRD v1.0 Section 12).
Measures:
1. Query Speed across 1M and 10M profiles (4 endpoints, 10 tenants, 5 warm-up + 30 timed runs).
2. Growth Ratio (10M / 1M).
3. Freshness probe latency (50 sequential inserts, polling API count).
4. DB Safety (alternating OLTP throughput under baseline, triggers on, and concurrent load).
5. Storage footprint & compression ratio.
6. Evaluates against PRD Section 1 Good/Great criteria and generates docs/BENCHMARK.md.
"""
import sys, os, time, datetime, platform, statistics, subprocess, threading, queue, argparse
import httpx, psycopg2
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

# ---------------------------------------------------------------------------
# PRD Targets & Thresholds (PRD v1.0 Section 1)
# ---------------------------------------------------------------------------
# OLTP write overhead: Good <= 20%, Great <= 10%
# Query latency p95: Good <= 100ms, Great <= 20ms
# Query scale factor: Good <= 3x, Great <= 1.5x (flat)
# Freshness lag p95: Good <= 1.0s, Great <= 0.5s
# Rollup size ratio: Good <= 15%, Great <= 8%

def get_hardware_info() -> Dict[str, str]:
    import ctypes
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
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    ram_gb = stat.ullTotalPhys / (1024 ** 3)

    # Git commit
    try:
        git_hash = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except Exception:
        git_hash = "unknown"

    return {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu_count": str(os.cpu_count()),
        "ram_gb": f"{ram_gb:.1f} GB",
        "pg_version": "PostgreSQL 16.8 (Alpine Docker)",
        "git_commit": git_hash,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }

# ---------------------------------------------------------------------------
# API Process Lifecycle Management
# ---------------------------------------------------------------------------
api_proc = None

def set_api_profile(profile: str):
    global api_proc
    if api_proc and api_proc.poll() is None:
        api_proc.terminate()
        api_proc.wait()
        api_proc = None

    # Kill anything currently holding port 8000
    subprocess.run([
        "powershell", "-Command",
        "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
    ], capture_output=True)
    time.sleep(1.0)

    env = os.environ.copy()
    env["PROFILE"] = profile
    api_proc = subprocess.Popen(["py", "-3.12", "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8000"], env=env)

    # Wait for healthcheck
    t0 = time.time()
    while time.time() - t0 < 15.0:
        try:
            r = httpx.get("http://127.0.0.1:8000/health", timeout=1.0)
            if r.status_code == 200:
                time.sleep(0.5)
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"API server failed to start on profile {profile}")

def stop_api():
    global api_proc
    if api_proc and api_proc.poll() is None:
        api_proc.terminate()
        api_proc.wait()
        api_proc = None

# ---------------------------------------------------------------------------
# 1. Query Speed Benchmark
# ---------------------------------------------------------------------------
def benchmark_queries(profile: str, base_url: str = "http://127.0.0.1:8000") -> Dict[str, Dict[str, float]]:
    set_api_profile(profile)
    print(f"\n--- Running Query Speed Benchmark ({profile.upper()}) ---")
    client = httpx.Client(base_url=base_url, timeout=30.0)
    tenants = [1, 99, 42, 77, 2, 5, 10, 25, 50, 90]
    f_time = "2026-10-01 00:00:00"
    t_time = "2026-10-30 00:00:00"

    endpoints = [
        ("count", f"/tenants/{{tid}}/orders/count?from={f_time}&to={t_time}"),
        ("trend_hour", f"/tenants/{{tid}}/orders/trend?from={f_time}&to={t_time}&grain=hour"),
        ("by_status", f"/tenants/{{tid}}/orders/by-status?from={f_time}&to={t_time}"),
        ("top_products", f"/tenants/{{tid}}/products/top?from={f_time}&to={t_time}&n=10&by=sales"),
    ]

    results: Dict[str, List[float]] = {name: [] for name, _ in endpoints}

    for name, path_tmpl in endpoints:
        print(f"  Benchmarking endpoint: {name} ...", end="", flush=True)
        # Warm-up (5 runs)
        for _ in range(5):
            for tid in tenants:
                url = path_tmpl.format(tid=tid)
                client.get(url, headers={"X-Tenant-Key": f"tenant-key-{tid}"})

        # 30 timed runs across 10 tenants (300 requests)
        latencies = []
        for _ in range(30):
            for tid in tenants:
                url = path_tmpl.format(tid=tid)
                t0 = time.perf_counter()
                r = client.get(url, headers={"X-Tenant-Key": f"tenant-key-{tid}"})
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if r.status_code == 200:
                    latencies.append(elapsed_ms)
        results[name] = sorted(latencies)
        p50 = statistics.median(results[name])
        p95 = results[name][int(len(results[name]) * 0.95)]
        max_v = max(results[name])
        print(f"  p50={p50:.2f}ms | p95={p95:.2f}ms | max={max_v:.2f}ms")

    client.close()

    summary = {}
    for name, lats in results.items():
        summary[name] = {
            "p50": statistics.median(lats),
            "p95": lats[int(len(lats) * 0.95)],
            "max": max(lats)
        }
    return summary

# ---------------------------------------------------------------------------
# 2. Freshness Probe Benchmark
# ---------------------------------------------------------------------------
def benchmark_freshness(main_url: str, base_url: str = "http://127.0.0.1:8000", count: int = 50) -> Dict[str, float]:
    print(f"\n--- Running Freshness Probe Benchmark ({count} orders) ---")
    client = httpx.Client(base_url=base_url, timeout=10.0)
    main_conn = psycopg2.connect(main_url)
    main_conn.autocommit = True
    main_cur = main_conn.cursor()

    tid = 1
    headers = {"X-Tenant-Key": f"tenant-key-{tid}"}
    base_id = 999_777_000

    # Ensure baseline count
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    f_time = (now_utc - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:00:00")
    t_time = (now_utc + datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:00:00")

    lags_ms = []

    for i in range(count):
        oid = base_id + i
        # Query current count
        r0 = client.get(f"/tenants/{tid}/orders/count?from={f_time}&to={t_time}", headers=headers)
        baseline_cnt = r0.json()["data"]["order_count"]

        # Insert one order with created_at = now()
        order_ts = datetime.datetime.now(datetime.timezone.utc)
        t_insert = time.perf_counter()
        main_cur.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, %s, 1, 'delivered', 99.00, %s, %s)
        """, (oid, tid, order_ts, order_ts))
        main_cur.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, %s, 1, 1, 99.00)
        """, (oid, oid, tid))

        # Poll API count until increment
        probe_start = time.perf_counter()
        seen = False
        while time.perf_counter() - probe_start < 5.0:
            r = client.get(f"/tenants/{tid}/orders/count?from={f_time}&to={t_time}", headers=headers)
            if r.json()["data"]["order_count"] > baseline_cnt:
                seen = True
                break
            time.sleep(0.01)

        lag_ms = (time.perf_counter() - t_insert) * 1000.0
        lags_ms.append(lag_ms)

    # Clean up test orders
    for i in range(count):
        oid = base_id + i
        main_cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
        main_cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))

    main_cur.close()
    main_conn.close()
    client.close()

    lags_ms.sort()
    res = {
        "p50": statistics.median(lags_ms),
        "p95": lags_ms[int(len(lags_ms) * 0.95)],
        "max": max(lags_ms)
    }
    print(f"  Freshness Latency: p50={res['p50']:.1f}ms | p95={res['p95']:.1f}ms | max={res['max']:.1f}ms")
    return res

# ---------------------------------------------------------------------------
# 3. DB Safety (Alternating OLTP Benchmark)
# ---------------------------------------------------------------------------
def run_oltp_worker(main_url: str, stop_event: threading.Event, latencies: List[float], counter: List[int], start_id: int):
    conn = psycopg2.connect(main_url)
    conn.autocommit = True
    cur = conn.cursor()
    curr_id = start_id

    while not stop_event.is_set():
        t0 = time.perf_counter()
        now_ts = datetime.datetime.now(datetime.timezone.utc)
        try:
            cur.execute("""
                INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                VALUES (%s, 1, 1, 'delivered', 10.00, %s, %s)
            """, (curr_id, now_ts, now_ts))
            cur.execute("""
                INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                VALUES (%s, %s, 1, 1, 1, 10.00)
            """, (curr_id, curr_id))
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed_ms)
            counter[0] += 1
            curr_id += 1
        except Exception as e:
            time.sleep(0.01)
    
    # Cleanup inserted rows
    try:
        cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id < %s", (start_id, curr_id))
        cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id < %s", (start_id, curr_id))
    except Exception:
        pass
    cur.close()
    conn.close()

def run_api_hammer(base_url: str, stop_event: threading.Event):
    client = httpx.Client(base_url=base_url, timeout=5.0)
    endpoints = [
        "/tenants/1/orders/count?from=2026-10-01&to=2026-10-30",
        "/tenants/1/orders/trend?from=2026-10-01&to=2026-10-30&grain=hour",
        "/tenants/1/orders/by-status?from=2026-10-01&to=2026-10-30",
        "/tenants/1/products/top?from=2026-10-01&to=2026-10-30&n=10&by=sales"
    ]
    idx = 0
    while not stop_event.is_set():
        try:
            client.get(endpoints[idx % 4], headers={"X-Tenant-Key": "tenant-key-1"})
            idx += 1
        except Exception:
            pass
        time.sleep(0.005)
    client.close()

def benchmark_db_safety(main_url: str, rollup_url: str, base_url: str = "http://127.0.0.1:8000", round_duration: float = 15.0) -> Dict[str, Dict]:
    print(f"\n--- Running DB Safety Benchmark (3 Alternating Rounds, {round_duration}s per step) ---")
    modes = ["a_baseline", "b_triggers_worker", "c_triggers_worker_api"]
    round_stats = {m: {"rates": [], "p50": [], "p95": [], "p99": []} for m in modes}

    # Helper to toggle triggers
    conn = psycopg2.connect(main_url)
    conn.autocommit = True
    cur = conn.cursor()

    def set_triggers(enable: bool):
        action = "ENABLE" if enable else "DISABLE"
        cur.execute(f"ALTER TABLE orders {action} TRIGGER trg_orders_insert")
        cur.execute(f"ALTER TABLE orders {action} TRIGGER trg_orders_delete")
        cur.execute(f"ALTER TABLE orders {action} TRIGGER trg_orders_update")
        cur.execute(f"ALTER TABLE order_items {action} TRIGGER trg_order_items_insert")
        cur.execute(f"ALTER TABLE order_items {action} TRIGGER trg_order_items_delete")

    worker_proc = None

    def start_worker():
        nonlocal worker_proc
        if worker_proc is None or worker_proc.poll() is not None:
            worker_proc = subprocess.Popen(["py", "-3.12", "worker/worker.py", main_url, rollup_url])
            time.sleep(0.5)

    def stop_worker():
        nonlocal worker_proc
        if worker_proc and worker_proc.poll() is None:
            worker_proc.terminate()
            worker_proc.wait()
            worker_proc = None

    base_id = 999_000_000

    try:
        for r_num in range(1, 4):
            print(f"\n  Round {r_num} of 3:")
            for mode in modes:
                stop_event = threading.Event()
                latencies = []
                counter = [0]
                hammer_threads = []

                if mode == "a_baseline":
                    print("    Running (a) Baseline: Triggers OFF, Worker OFF ...", flush=True)
                    stop_worker()
                    set_triggers(False)
                elif mode == "b_triggers_worker":
                    print("    Running (b) Triggers ON, Worker ON ...", flush=True)
                    set_triggers(True)
                    start_worker()
                elif mode == "c_triggers_worker_api":
                    print("    Running (c) Triggers ON, Worker ON + 8 Concurrent API Hammers ...", flush=True)
                    set_triggers(True)
                    start_worker()
                    for _ in range(8):
                        t = threading.Thread(target=run_api_hammer, args=(base_url, stop_event))
                        t.daemon = True
                        t.start()
                        hammer_threads.append(t)

                # Execute OLTP generator
                t_oltp = threading.Thread(target=run_oltp_worker, args=(main_url, stop_event, latencies, counter, base_id))
                base_id += 50000
                t0 = time.time()
                t_oltp.start()

                time.sleep(round_duration)
                stop_event.set()
                t_oltp.join()
                for t in hammer_threads:
                    t.join()

                dur = time.time() - t0
                w_rate = counter[0] / dur
                latencies.sort()
                p50 = statistics.median(latencies) if latencies else 0.0
                p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
                p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0.0

                round_stats[mode]["rates"].append(w_rate)
                round_stats[mode]["p50"].append(p50)
                round_stats[mode]["p95"].append(p95)
                round_stats[mode]["p99"].append(p99)
                print(f"      Result: {w_rate:.1f} writes/s | p50={p50:.2f}ms | p95={p95:.2f}ms | p99={p99:.2f}ms")

    finally:
        set_triggers(True)
        stop_worker()
        cur.close()
        conn.close()

    # Aggregate means
    summary = {}
    for m in modes:
        summary[m] = {
            "rate": statistics.mean(round_stats[m]["rates"]),
            "p50": statistics.mean(round_stats[m]["p50"]),
            "p95": statistics.mean(round_stats[m]["p95"]),
            "p99": statistics.mean(round_stats[m]["p99"]),
        }
    return summary

# ---------------------------------------------------------------------------
# 4. Storage Footprint
# ---------------------------------------------------------------------------
def get_storage_stats(profile: str) -> Dict[str, float]:
    cfg = get_config(profile)
    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_db_url"]

    conn_m = psycopg2.connect(main_url)
    cur_m = conn_m.cursor()
    cur_m.execute("SELECT sum(pg_total_relation_size(c.oid)) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'")
    main_raw_bytes = cur_m.fetchone()[0] or 0
    cur_m.close()
    conn_m.close()

    conn_r = psycopg2.connect(rollup_url)
    cur_r = conn_r.cursor()
    cur_r.execute("SELECT sum(pg_total_relation_size(c.oid)) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'")
    rollup_bytes = cur_r.fetchone()[0] or 0
    cur_r.close()
    conn_r.close()

    main_mb = float(main_raw_bytes) / (1024 * 1024)
    rollup_mb = float(rollup_bytes) / (1024 * 1024)
    return {
        "raw_mb": main_mb,
        "rollup_mb": rollup_mb,
        "ratio_pct": (rollup_mb / main_mb) * 100.0 if main_mb else 0.0
    }

# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------
def run_full_benchmark(oltp_duration: float = 10.0):
    hw = get_hardware_info()
    print("=" * 70)
    print("CYBERFORGE OFFICIAL BENCHMARK BATTERY (PRD SECTION 12)")
    print(f"Hardware: {hw['cpu_count']} CPUs, {hw['ram_gb']} RAM on {hw['os']}")
    print(f"Commit: {hw['git_commit']} | Timestamp: {hw['timestamp']}")
    print("=" * 70)

    # 1. 1M Queries
    q_1m = benchmark_queries("1m")

    # 2. 10M Queries
    q_10m = benchmark_queries("10m")

    # 3. Freshness on 1M
    set_api_profile("1m")
    p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", "postgresql://postgres:postgres@localhost:5432/shop", "postgresql://postgres:postgres@localhost:5433/rollup_1m"])
    time.sleep(1.0)
    try:
        f_1m = benchmark_freshness("postgresql://postgres:postgres@localhost:5432/shop")
    finally:
        p_worker.terminate()
        p_worker.wait()

    # 4. DB Safety on 1M
    safety = benchmark_db_safety(
        "postgresql://postgres:postgres@localhost:5432/shop",
        "postgresql://postgres:postgres@localhost:5433/rollup_1m",
        round_duration=oltp_duration
    )
    stop_api()

    # 5. Storage Stats
    stor_1m = get_storage_stats("1m")
    stor_10m = get_storage_stats("10m")

    # 6. Evaluation against PRD criteria
    evaluations = []

    # Query speed
    all_p95_1m = [v["p95"] for v in q_1m.values()]
    max_p95_1m = max(all_p95_1m)
    all_p95_10m = [v["p95"] for v in q_10m.values()]
    max_p95_10m = max(all_p95_10m)

    # Growth factor
    growth_factors = [q_10m[k]["p50"] / max(0.001, q_1m[k]["p50"]) for k in q_1m]
    mean_growth = statistics.mean(growth_factors)

    # OLTP overhead
    base_rate = safety["a_baseline"]["rate"]
    trg_rate = safety["b_triggers_worker"]["rate"]
    ham_rate = safety["c_triggers_worker_api"]["rate"]
    drop_trg_pct = ((base_rate - trg_rate) / base_rate) * 100.0 if base_rate else 0.0
    drop_ham_pct = ((base_rate - ham_rate) / base_rate) * 100.0 if base_rate else 0.0

    # Freshness
    fresh_p95 = f_1m["p95"] / 1000.0  # seconds

    evaluations.append({
        "metric": "Query Latency p95 (10M)",
        "measured": f"{max_p95_10m:.2f} ms",
        "good": "<= 100 ms",
        "great": "<= 20 ms",
        "status": "GREAT" if max_p95_10m <= 20 else ("GOOD" if max_p95_10m <= 100 else "FAIL")
    })
    evaluations.append({
        "metric": "Query Scale Factor (10M / 1M)",
        "measured": f"{mean_growth:.2f}x",
        "good": "<= 3.0x",
        "great": "<= 1.5x (flat)",
        "status": "GREAT" if mean_growth <= 1.5 else ("GOOD" if mean_growth <= 3.0 else "FAIL")
    })
    evaluations.append({
        "metric": "Freshness Lag p95",
        "measured": f"{fresh_p95:.3f} s ({fresh_p95*1000:.1f} ms)",
        "good": "<= 1.0 s",
        "great": "<= 0.5 s",
        "status": "GREAT" if fresh_p95 <= 0.5 else ("GOOD" if fresh_p95 <= 1.0 else "FAIL")
    })
    evaluations.append({
        "metric": "OLTP Write Overhead (Triggers+Worker)",
        "measured": f"{drop_trg_pct:.1f}% drop",
        "good": "<= 20%",
        "great": "<= 10%",
        "status": "GREAT" if drop_trg_pct <= 10 else ("GOOD" if drop_trg_pct <= 20 else "FAIL")
    })
    evaluations.append({
        "metric": "OLTP Write Overhead (+Concurrent API)",
        "measured": f"{drop_ham_pct:.1f}% drop",
        "good": "<= 25%",
        "great": "<= 15%",
        "status": "GREAT" if drop_ham_pct <= 15 else ("GOOD" if drop_ham_pct <= 25 else "FAIL")
    })
    evaluations.append({
        "metric": "Rollup Storage Ratio (10M)",
        "measured": f"{stor_10m['ratio_pct']:.2f}%",
        "good": "<= 15%",
        "great": "<= 8%",
        "status": "GREAT" if stor_10m['ratio_pct'] <= 8 else ("GOOD" if stor_10m['ratio_pct'] <= 15 else "FAIL")
    })

    # Print Report
    print("\n" + "=" * 75)
    print("FINAL BENCHMARK SCOREBOARD & EVALUATION (PRD SECTION 1)")
    print("=" * 75)
    print(f"{'Metric':<40} | {'Measured':<18} | {'Target':<10} | {'Status'}")
    print("-" * 75)
    for e in evaluations:
        print(f"{e['metric']:<40} | {e['measured']:<18} | {e['good']:<10} | {e['status']}")
    print("=" * 75)

    # Save to docs/BENCHMARK.md
    report_md = f"""# Benchmark Report: Multi-Tenant Aggregation Layer
**Generated:** {hw['timestamp']}  
**Hardware:** {hw['cpu_count']} vCPUs, {hw['ram_gb']} RAM, {hw['os']}  
**Database:** {hw['pg_version']}  
**Git Commit:** `{hw['git_commit']}`  

---

## 1. Executive Summary & PRD Scoreboard

| Metric | Measured | Good Target | Great Target | Status |
| :--- | :--- | :--- | :--- | :--- |
"""
    for e in evaluations:
        report_md += f"| **{e['metric']}** | `{e['measured']}` | `{e['good']}` | `{e['great']}` | **{e['status']}** |\n"

    report_md += f"""
---

## 2. Query Latency Breakdown (API HTTP localhost)

### 1M Dataset (shop / rollup_1m)
- 10 sample tenants across 30 timed iterations per endpoint (300 calls/endpoint):
| Endpoint | p50 | p95 | max |
| :--- | :--- | :--- | :--- |
| `/orders/count` | {q_1m['count']['p50']:.2f} ms | {q_1m['count']['p95']:.2f} ms | {q_1m['count']['max']:.2f} ms |
| `/orders/trend (hour)` | {q_1m['trend_hour']['p50']:.2f} ms | {q_1m['trend_hour']['p95']:.2f} ms | {q_1m['trend_hour']['max']:.2f} ms |
| `/orders/by-status` | {q_1m['by_status']['p50']:.2f} ms | {q_1m['by_status']['p95']:.2f} ms | {q_1m['by_status']['max']:.2f} ms |
| `/products/top (sales)` | {q_1m['top_products']['p50']:.2f} ms | {q_1m['top_products']['p95']:.2f} ms | {q_1m['top_products']['max']:.2f} ms |

### 10M Dataset (shop_10m / rollup_10m)
- 10 sample tenants across 30 timed iterations per endpoint:
| Endpoint | p50 | p95 | max | Scale Factor (10M/1M) |
| :--- | :--- | :--- | :--- | :--- |
| `/orders/count` | {q_10m['count']['p50']:.2f} ms | {q_10m['count']['p95']:.2f} ms | {q_10m['count']['max']:.2f} ms | {q_10m['count']['p50']/max(0.001, q_1m['count']['p50']):.2f}x |
| `/orders/trend (hour)` | {q_10m['trend_hour']['p50']:.2f} ms | {q_10m['trend_hour']['p95']:.2f} ms | {q_10m['trend_hour']['max']:.2f} ms | {q_10m['trend_hour']['p50']/max(0.001, q_1m['trend_hour']['p50']):.2f}x |
| `/orders/by-status` | {q_10m['by_status']['p50']:.2f} ms | {q_10m['by_status']['p95']:.2f} ms | {q_10m['by_status']['max']:.2f} ms | {q_10m['by_status']['p50']/max(0.001, q_1m['by_status']['p50']):.2f}x |
| `/products/top (sales)` | {q_10m['top_products']['p50']:.2f} ms | {q_10m['top_products']['p95']:.2f} ms | {q_10m['top_products']['max']:.2f} ms | {q_10m['top_products']['p50']/max(0.001, q_1m['top_products']['p50']):.2f}x |

---

## 3. Freshness Probe Latency
50 sequential orders inserted with `created_at = now()`, immediately polling API count until increment:
- **p50:** `{f_1m['p50']:.1f} ms`
- **p95:** `{f_1m['p95']:.1f} ms`
- **max:** `{f_1m['max']:.1f} ms`

---

## 4. DB Safety & OLTP Write Overhead
Averaged over 3 alternating rounds (A-B-C-A-B-C-A-B-C):
| Scenario | Throughput | p50 Latency | p95 Latency | p99 Latency | Degradation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **(a) Baseline (Triggers OFF, Worker OFF)** | {safety['a_baseline']['rate']:.1f} writes/s | {safety['a_baseline']['p50']:.2f} ms | {safety['a_baseline']['p95']:.2f} ms | {safety['a_baseline']['p99']:.2f} ms | Baseline |
| **(b) Triggers ON, Worker ON** | {safety['b_triggers_worker']['rate']:.1f} writes/s | {safety['b_triggers_worker']['p50']:.2f} ms | {safety['b_triggers_worker']['p95']:.2f} ms | {safety['b_triggers_worker']['p99']:.2f} ms | **{drop_trg_pct:.1f}% drop** |
| **(c) Triggers ON + Worker + 8 API Clients** | {safety['c_triggers_worker_api']['rate']:.1f} writes/s | {safety['c_triggers_worker_api']['p50']:.2f} ms | {safety['c_triggers_worker_api']['p95']:.2f} ms | {safety['c_triggers_worker_api']['p99']:.2f} ms | **{drop_ham_pct:.1f}% drop** |

---

## 5. Storage Footprint & Compression

| Profile | Raw Main-DB Size | Rollup-DB Size | Rollup Ratio vs Raw |
| :--- | :--- | :--- | :--- |
| **1M Dataset** | {stor_1m['raw_mb']:.1f} MB | {stor_1m['rollup_mb']:.1f} MB | **{stor_1m['ratio_pct']:.2f}%** |
| **10M Dataset** | {stor_10m['raw_mb']:.1f} MB | {stor_10m['rollup_mb']:.1f} MB | **{stor_10m['ratio_pct']:.2f}%** |
"""
    with open("docs/BENCHMARK.md", "w", encoding="utf-8") as f:
        f.write(report_md)
    print("\nBenchmark report successfully written to docs/BENCHMARK.md")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--oltp-duration", type=float, default=10.0, help="Duration in seconds per OLTP step")
    args = parser.parse_args()
    run_full_benchmark(oltp_duration=args.oltp_duration)
