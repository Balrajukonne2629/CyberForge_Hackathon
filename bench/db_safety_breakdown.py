#!/usr/bin/env python3
"""bench/db_safety_breakdown.py:
Precise DB Safety Breakdown benchmark measuring:
- Normal OLTP query latency (p50, p95, p99 ms)
- Write throughput (writes/sec)
Across 4 configurations:
  (a) no triggers, no worker, no dashboard load
  (b) triggers on, worker off
  (c) triggers on, worker on
  (d) triggers on, worker on, 8 concurrent dashboard clients
Runs 5 repeats each in alternating order (a, b, c, d x 5).
Reports median and noise level (stddev / IQR).
"""
import sys, os, time, datetime, random, threading, subprocess, statistics
from typing import Dict, List, Tuple
import numpy as np
import httpx
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url, get_all_tenant_ids

from concurrent.futures import ThreadPoolExecutor

def set_triggers(enabled: bool, profile: str = "1m"):
    """Enable or disable triggers across all 100 tenant DBs in parallel."""
    action = "ENABLE" if enabled else "DISABLE"
    def toggle_tenant(tid: int):
        try:
            url = get_tenant_db_url(tid, profile)
            c = psycopg2.connect(url)
            c.autocommit = True
            cur = c.cursor()
            cur.execute(f"ALTER TABLE orders {action} TRIGGER ALL")
            cur.execute(f"ALTER TABLE order_items {action} TRIGGER ALL")
            cur.close()
            c.close()
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(toggle_tenant, range(1, 101)))

def run_dashboard_client(stop_event: threading.Event, base_url: str = "http://127.0.0.1:8000"):
    """Simulate a continuous dashboard user querying endpoints."""
    client = httpx.Client(base_url=base_url, timeout=5.0)
    tenants = [1, 2, 5, 10, 25, 42, 50, 77, 90, 99]
    while not stop_event.is_set():
        tid = random.choice(tenants)
        ep = random.choice([
            f"/tenants/{tid}/orders/count?from=2026-10-01&to=2026-10-30",
            f"/tenants/{tid}/orders/trend?from=2026-10-01&to=2026-10-30&grain=hour",
            f"/tenants/{tid}/orders/by-status?from=2026-10-01&to=2026-10-30",
            f"/tenants/{tid}/products/top?from=2026-10-01&to=2026-10-30&n=10&by=sales"
        ])
        try:
            client.get(ep, headers={"X-Tenant-Key": f"tenant-key-{tid}"})
        except Exception:
            pass
        time.sleep(0.01)
    client.close()

def run_single_measurement(
    profile: str,
    duration: float,
    run_label: str,
    has_triggers: bool,
    has_worker: bool,
    has_dashboard: bool
) -> Dict[str, float]:
    # 1. Trigger state
    set_triggers(has_triggers, profile)

    # 2. Worker process
    p_worker = None
    if has_worker:
        p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile])
        time.sleep(1.0)

    # 3. Dashboard clients (in a separate subprocess to avoid Python GIL contention)
    p_dashboard = None
    if has_dashboard:
        p_dashboard = subprocess.Popen(["py", "-3.12", "bench/dashboard_stress.py"])
        time.sleep(0.5)

    # 4. Measure OLTP query latency and write throughput concurrently
    oltp_latencies = []
    write_count = [0]
    stop_measurement = threading.Event()

    # OLTP Probe thread (80% point reads, 20% point updates)
    def oltp_worker():
        rng = random.Random(42 + int(time.time()))
        tid = 1  # target representative tenant
        url = get_tenant_db_url(tid, profile)
        c = psycopg2.connect(url)
        c.autocommit = True
        cur = c.cursor()
        cur.execute("SELECT coalesce(max(order_id), 1) FROM orders")
        mx = cur.fetchone()[0]

        while not stop_measurement.is_set():
            oid = rng.randint(1, mx)
            t0 = time.perf_counter()
            if rng.random() < 0.8:
                cur.execute("""
                    SELECT o.order_id, o.status, o.total, i.product_id, i.qty, i.price
                    FROM orders o
                    JOIN order_items i ON o.order_id = i.order_id
                    WHERE o.order_id = %s
                """, (oid,))
                cur.fetchall()
            else:
                cur.execute("UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s", (oid,))
            lat = (time.perf_counter() - t0) * 1000.0
            oltp_latencies.append(lat)
        cur.close()
        c.close()

    # Write worker thread (new orders + items)
    base_write_id = int(time.time() * 1000) % 900_000_000 + 70_000_000
    cleaned_ids = []

    def write_worker():
        tid = 1
        url = get_tenant_db_url(tid, profile)
        c = psycopg2.connect(url)
        c.autocommit = True
        cur = c.cursor()
        curr_oid = base_write_id

        while not stop_measurement.is_set():
            curr_oid += 1
            now_ts = datetime.datetime.now(datetime.timezone.utc)
            try:
                cur.execute("""
                    INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                    VALUES (%s, %s, 1, 'delivered', 25.00, %s, %s)
                """, (curr_oid, tid, now_ts, now_ts))
                cur.execute("""
                    INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                    VALUES (%s, %s, %s, 1, 1, 25.00)
                """, (curr_oid, curr_oid, tid))
                write_count[0] += 1
                cleaned_ids.append(curr_oid)
            except Exception:
                pass
        cur.close()
        c.close()

    t_oltp = threading.Thread(target=oltp_worker)
    t_write = threading.Thread(target=write_worker)

    t0_all = time.perf_counter()
    t_oltp.start()
    t_write.start()

    time.sleep(duration)
    stop_measurement.set()
    t_oltp.join()
    t_write.join()
    total_sec = time.perf_counter() - t0_all

    # Stop dashboard & worker
    if p_dashboard:
        p_dashboard.terminate()
        p_dashboard.wait()

    if p_worker:
        p_worker.terminate()
        p_worker.wait()

    # Clean up test rows
    if cleaned_ids:
        url = get_tenant_db_url(1, profile)
        c = psycopg2.connect(url)
        c.autocommit = True
        cur = c.cursor()
        cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id <= %s", (min(cleaned_ids), max(cleaned_ids)))
        cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id <= %s", (min(cleaned_ids), max(cleaned_ids)))
        cur.close()
        c.close()

    # Re-enable triggers if disabled
    if not has_triggers:
        set_triggers(True, profile)

    # Compute percentiles
    p50, p95, p99 = np.percentile(oltp_latencies, [50, 95, 99]) if oltp_latencies else (0, 0, 0)
    w_tput = write_count[0] / total_sec

    return {
        "p50": float(p50),
        "p95": float(p95),
        "p99": float(p99),
        "write_tput": float(w_tput),
        "ops_count": len(oltp_latencies)
    }

def run_benchmark_breakdown(profile: str = "1m", repeats: int = 5, duration: float = 4.0):
    print("=" * 80)
    print(f"RUNNING DB SAFETY BREAKDOWN (5 ALTERNATING REPEATS, PROFILE: {profile.upper()})")
    print(f"Configurations:")
    print(f"  (a) no triggers, no worker, no dashboard load")
    print(f"  (b) triggers on, worker off")
    print(f"  (c) triggers on, worker on")
    print(f"  (d) triggers on, worker on, 8 concurrent dashboard clients")
    print("=" * 80)

    # Start API if not running
    p_api = None
    try:
        httpx.get("http://127.0.0.1:8000/health", timeout=1.0)
    except Exception:
        print("Starting FastAPI server for dashboard client simulation...")
        p_api = subprocess.Popen(["py", "-3.12", "-m", "uvicorn", "api.main:app", "--port", "8000"])
        time.sleep(2.0)

    try:
        configs = [
            ("a", "No Triggers, Worker OFF, Idle", False, False, False),
            ("b", "Triggers ON, Worker OFF", True, False, False),
            ("c", "Triggers ON, Worker ON", True, True, False),
            ("d", "Triggers ON, Worker ON, 8 Dash Clients", True, True, True),
        ]

        raw_results = {key: [] for key, _, _, _, _ in configs}

        for rep in range(1, repeats + 1):
            print(f"\n--- Repeat {rep}/{repeats} (Alternating Order: a -> b -> c -> d) ---")
            for key, name, has_trg, has_wrk, has_dsh in configs:
                print(f"  Measuring ({key}) {name} ({duration}s)... ", end="", flush=True)
                res = run_single_measurement(profile, duration, key, has_trg, has_wrk, has_dsh)
                raw_results[key].append(res)
                print(f"p50={res['p50']:.2f}ms | p95={res['p95']:.2f}ms | p99={res['p99']:.2f}ms | writes={res['write_tput']:.1f}/s")

        print("\n" + "=" * 80)
        print(f"MEDIAN RESULTS & NOISE LEVEL (OVER {repeats} ALTERNATING REPEATS):")
        print("=" * 80)

        stats_summary = {}
        for key, name, _, _, _ in configs:
            p50s = [r["p50"] for r in raw_results[key]]
            p95s = [r["p95"] for r in raw_results[key]]
            p99s = [r["p99"] for r in raw_results[key]]
            tputs = [r["write_tput"] for r in raw_results[key]]

            stats_summary[key] = {
                "name": name,
                "p50_median": statistics.median(p50s),
                "p50_std": statistics.stdev(p50s) if len(p50s) > 1 else 0.0,
                "p95_median": statistics.median(p95s),
                "p95_std": statistics.stdev(p95s) if len(p95s) > 1 else 0.0,
                "p99_median": statistics.median(p99s),
                "p99_std": statistics.stdev(p99s) if len(p99s) > 1 else 0.0,
                "tput_median": statistics.median(tputs),
                "tput_std": statistics.stdev(tputs) if len(tputs) > 1 else 0.0,
            }

        header = f"{'Configuration':<42} | {'p50 (ms)':<10} | {'p95 (ms)':<10} | {'p99 (ms)':<10} | {'Write tput (w/s)':<16} | {'Noise (p95 std)':<15}"
        print(header)
        print("-" * len(header))
        for key in ["a", "b", "c", "d"]:
            s = stats_summary[key]
            print(f"({key}) {s['name']:<38} | {s['p50_median']:>6.2f} ms  | {s['p95_median']:>6.2f} ms  | {s['p99_median']:>6.2f} ms  | {s['tput_median']:>8.1f} w/s      | ±{s['p95_std']:>5.2f} ms")

        # Comparisons against baseline (a)
        base_p95 = stats_summary["a"]["p95_median"]
        base_tput = stats_summary["a"]["tput_median"]

        print("\n" + "=" * 80)
        print("IMPACT / OVERHEAD BREAKDOWN RELATIVE TO BASELINE (a):")
        print("=" * 80)
        for key in ["b", "c", "d"]:
            s = stats_summary[key]
            p95_diff = ((s["p95_median"] - base_p95) / base_p95) * 100.0
            tput_diff = ((base_tput - s["tput_median"]) / base_tput) * 100.0
            print(f"({key}) {s['name']:<38} -> OLTP p95 slowdown: {p95_diff:>+6.1f}% | Write tput reduction: {tput_diff:>5.1f}%")

        # Compare (c) vs (d) for Dashboard isolation impact
        c_p95 = stats_summary["c"]["p95_median"]
        d_p95 = stats_summary["d"]["p95_median"]
        dash_impact = ((d_p95 - c_p95) / c_p95) * 100.0
        print(f"\nDashboard Load Isolation: (d) vs (c) OLTP query slowdown: {dash_impact:>+5.1f}% (PRD Target: < 10% Great, < 25% Good)")

    finally:
        # Ensure triggers are left enabled
        set_triggers(True, profile)
        if p_api and p_api.poll() is None:
            p_api.terminate()
            p_api.wait()
    return stats_summary

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    run_benchmark_breakdown(profile=prof, repeats=5, duration=4.0)
