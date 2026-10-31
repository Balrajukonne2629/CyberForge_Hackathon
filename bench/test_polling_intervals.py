#!/usr/bin/env python3
"""bench/test_polling_intervals.py:
Tests worker idle polling intervals: 100ms, 500ms, 1000ms.
Measures:
1. Freshness p95 latency (20 probes across random tenants).
2. OLTP p95 query latency and write throughput during worker on.
"""
import sys, os, time, subprocess, statistics, random, datetime, httpx, psycopg2
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from bench.bench import benchmark_freshness

def measure_oltp_under_worker(profile: str = "1m", duration: float = 3.0):
    url = get_tenant_db_url(1, profile)
    c = psycopg2.connect(url)
    c.autocommit = True
    cur = c.cursor()
    cur.execute("SELECT coalesce(max(order_id), 1) FROM orders")
    mx = cur.fetchone()[0]

    latencies = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration:
        oid = random.randint(1, mx)
        t_start = time.perf_counter()
        cur.execute("""
            SELECT o.order_id, o.status, o.total, i.product_id, i.qty, i.price
            FROM orders o JOIN order_items i ON o.order_id = i.order_id
            WHERE o.order_id = %s
        """, (oid,))
        cur.fetchall()
        latencies.append((time.perf_counter() - t_start) * 1000.0)
    cur.close()
    c.close()
    return float(np.percentile(latencies, 95))

def main():
    profile = "1m"
    intervals = [0.10, 0.50, 1.00]
    print("=" * 70)
    print("IDLE POLLING INTERVAL COMPARISON (100ms vs 500ms vs 1000ms)")
    print("=" * 70)

    # Start API
    p_api = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--port", "8000"])
    time.sleep(2.0)

    results = []

    try:
        for interval in intervals:
            print(f"\n--- Testing poll_interval = {interval*1000:.0f} ms ---")
            p_worker = subprocess.Popen([
                "py", "-3.12", "worker/worker.py",
                "--profile", profile,
                "--poll-interval", str(interval)
            ])
            time.sleep(1.5)

            try:
                # 1. Measure OLTP query latency under this polling rate
                oltp_p95 = measure_oltp_under_worker(profile, duration=3.0)

                # 2. Measure freshness over 20 probes
                fresh = benchmark_freshness(profile, base_url="http://127.0.0.1:8000", count=20)
                f_p95 = fresh["p95"]

                print(f"  Result: Freshness p95 = {f_p95:.1f} ms | OLTP p95 = {oltp_p95:.2f} ms")
                results.append((interval, f_p95, oltp_p95))
            finally:
                p_worker.terminate()
                p_worker.wait()

        print("\n" + "=" * 70)
        print(f"{'Poll Interval':<15} | {'Freshness p95 (Goal <5s)':<25} | {'OLTP Query p95':<15} | {'Status'}")
        print("-" * 70)
        for interval, f_p95, oltp_p95 in results:
            status = "PASS (<5s)" if f_p95 < 5000.0 else "FAIL"
            print(f"{interval*1000:>6.0f} ms        | {f_p95:>10.1f} ms             | {oltp_p95:>8.2f} ms     | {status}")
        print("=" * 70)

    finally:
        p_api.terminate()
        p_api.wait()

if __name__ == "__main__":
    main()
