#!/usr/bin/env python3
"""bench/run_measurement_1m.py:
Standalone runner for Benchmark Item D on full 1M dataset.
Measures:
1. Query speed for all 4 endpoints (5 warm-up + 30 timed across 10 tenants = 300 requests/endpoint).
2. Freshness (50 sequential probes across random tenants).
3. Exact storage sizes across tenant databases and rollup database.
Prints full raw output and markdown tables.
"""
import sys, os, time, subprocess, httpx
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bench.bench import benchmark_queries, benchmark_freshness, get_hardware_info
from scripts.report_sizes import report_sizes

def main():
    profile = "1m"
    print("=" * 70)
    print("BENCHMARK ITEM D: FRESH MEASUREMENTS ON FULL 1M DATASET")
    print("Profile: 1m | Date:", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))
    print("=" * 70)

    # 1. Start API and Worker
    print("\n[1] Starting API and Worker daemon for profile 1m...")
    p_api = subprocess.Popen(["py", "-3.12", "-m", "uvicorn", "api.main:app", "--port", "8000"])
    p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile])

    # Wait for API ready
    client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=5.0)
    for _ in range(30):
        try:
            r = client.get("/health")
            if r.status_code == 200:
                print("  API is ready on http://127.0.0.1:8000")
                break
        except Exception:
            time.sleep(0.5)

    try:
        # 2. Query Speed Benchmark (4 endpoints, 5 warm-up + 30 timed across 10 tenants)
        print("\n[2] Query Speed Benchmark (5 warm-up + 30 timed across 10 tenants = 300 queries/endpoint):")
        query_results = benchmark_queries(profile=profile, base_url="http://127.0.0.1:8000")

        # 3. Freshness Benchmark (50 probes across random tenants)
        print("\n[3] Freshness Benchmark (50 sequential probes across random tenants):")
        freshness_results = benchmark_freshness(profile=profile, base_url="http://127.0.0.1:8000", count=50)

        # 4. Storage Footprint
        print("\n[4] Storage Footprint:")
        report_sizes()

        # 5. Summary Table
        print("\n" + "=" * 70)
        print("SUMMARY TABLE (MEASURED VALUES - PROFILE 1M)")
        print("=" * 70)
        print(f"{'Metric':<25} | {'p50':<10} | {'p95':<10} | {'p99 / Max':<12} | {'Goal / Status'}")
        print("-" * 75)
        for ep, vals in query_results.items():
            status = "PASS (<100ms)" if vals["p95"] < 100.0 else "FAIL"
            print(f"Query: {ep:<18} | {vals['p50']:>7.2f} ms | {vals['p95']:>7.2f} ms | {vals['max']:>9.2f} ms | {status}")
        f_status = "PASS (<5s)" if freshness_results["p95"] < 5000.0 else "FAIL"
        print(f"Freshness (50 probes)     | {freshness_results['p50']:>7.2f} ms | {freshness_results['p95']:>7.2f} ms | {freshness_results['max']:>9.2f} ms | {f_status}")
        print("=" * 70)

    finally:
        p_worker.terminate()
        p_api.terminate()
        p_worker.wait()
        p_api.wait()
        client.close()

if __name__ == "__main__":
    main()
