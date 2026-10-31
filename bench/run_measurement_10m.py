#!/usr/bin/env python3
"""bench/run_measurement_10m.py:
Comprehensive Benchmark Suite for Profile 10M (10M Orders across 100 Tenant Databases):
1. Dataset Integrity Tripwire (10M orders, 11,416,141 items).
2. Query Speed Benchmark across all 4 endpoints (5 warm-up + 30 timed across 10 tenants = 300 queries/endpoint).
3. Freshness Benchmark (50 sequential probes across random tenants).
4. Worker Cycle Time Benchmark at 100 tenants.
5. EXPLAIN (ANALYZE, BUFFERS) for top-N query on biggest tenant (Tenant 1, 30-day range).
6. Flood biggest tenant at 10M with 5,000 rows: measure drain time and observer tenants' lag over time.
7. Growth Ratio comparison (1M vs 10M) for p50 and p95 side-by-side.
8. Covering Index evaluation on rollup_product_day if top-N ratio > 1.5x.
"""
import sys, os, time, subprocess, datetime, httpx, psycopg2
from typing import Dict, List, Tuple
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from bench.bench import benchmark_queries, benchmark_freshness
from tests.integrity_tripwire import verify_integrity_tripwire
from worker.worker import MultiTenantWorker
from tests.sync import wait_for_worker

# 1M Reference values measured previously on this machine
REFERENCE_1M = {
    "/orders/count": {"p50": 4.80, "p95": 7.50},
    "/orders/trend (hour)": {"p50": 8.18, "p95": 13.58},
    "/orders/by-status": {"p50": 6.89, "p95": 9.83},
    "/products/top (sales)": {"p50": 7.47, "p95": 20.19},
}

def explain_analyze_top_n(profile: str = "10m", tenant_id: int = 1):
    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    conn = psycopg2.connect(rollup_url)
    cur = conn.cursor()

    query = """
    EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
    SELECT product_id, sum(sales) AS total_sales
    FROM rollup_product_day
    WHERE tenant_id = %s
      AND day >= '2026-10-01'::date AND day < '2026-10-31'::date
    GROUP BY product_id
    ORDER BY total_sales DESC
    LIMIT 10;
    """
    cur.execute(query, (tenant_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return "\n".join(r[0] for r in rows)

def run_flood_test_10m(profile: str = "10m", flood_count: int = 5000):
    print("\n" + "=" * 70)
    print(f"FLOODING BIGGEST TENANT (TENANT 1) WITH {flood_count:,} ROWS AT 10M")
    print("=" * 70)

    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    flood_tid = 1
    observer_tids = [2, 10, 42, 77, 99]
    fairness_hour = "2026-10-29 12:00:00"

    f_url = get_tenant_db_url(flood_tid, profile)
    f_conn = psycopg2.connect(f_url)
    f_conn.autocommit = True
    f_cur = f_conn.cursor()

    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()

    print(f"1. Injecting burst of {flood_count:,} outbox rows into Tenant {flood_tid}...")
    t0_flood = time.perf_counter()
    f_cur.execute("""
        INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
        SELECT 'order', 1, %s::timestamp, 'delivered', 1, 10.00
        FROM generate_series(1, %s)
        RETURNING id
    """, (fairness_hour, flood_count))
    flood_ids = [r[0] for r in f_cur.fetchall()]
    max_id = max(flood_ids)
    print(f"   Injected in {time.perf_counter() - t0_flood:.2f}s (max outbox id: {max_id})")

    # Monitor drain time and other tenants' lag over time
    print("2. Monitoring drain progression and observer tenants' lag over time...")
    t_start = time.perf_counter()
    lag_log = []

    while time.perf_counter() - t_start < 25.0:
        elapsed = time.perf_counter() - t_start
        # Check Tenant 1 cursor & pending
        r_cur.execute("SELECT cursor_id, lag_seconds, has_pending FROM worker_state WHERE tenant_id = 1")
        w1 = r_cur.fetchone()
        t1_cur = w1[0] if w1 else 0
        t1_lag = float(w1[1] or 0.0) if w1 else 0.0
        t1_pending = bool(w1[2]) if w1 else False

        # Sample observer tenant lags
        r_cur.execute("SELECT avg(coalesce(lag_seconds, 0)), max(coalesce(lag_seconds, 0)) FROM worker_state WHERE tenant_id != 1")
        obs_avg, obs_max = r_cur.fetchone()
        obs_avg, obs_max = float(obs_avg or 0.0), float(obs_max or 0.0)

        lag_log.append({
            "elapsed_s": round(elapsed, 2),
            "t1_cur": t1_cur,
            "t1_lag_s": t1_lag,
            "t1_pending": t1_pending,
            "other_avg_lag_s": round(obs_avg, 3),
            "other_max_lag_s": round(obs_max, 3)
        })

        if not t1_pending and t1_cur >= max_id:
            break
        time.sleep(0.1)

    drain_time = time.perf_counter() - t_start
    print(f"\n   >>> Drain completed in {drain_time:.2f}s <<<")

    # Sample table of progression
    print("\n   Lag progression during flood drain:")
    print(f"   {'Elapsed':<10} | {'Tenant 1 Cursor':<18} | {'Tenant 1 Lag':<15} | {'Other Avg Lag':<15} | {'Other Max Lag'}")
    print("   " + "-" * 75)
    sample_steps = lag_log[::max(1, len(lag_log)//6)] + [lag_log[-1]]
    for s in sample_steps:
        print(f"   {s['elapsed_s']:>6.2f}s    | {s['t1_cur']:>14,}   | {s['t1_lag_s']:>10.3f}s    | {s['other_avg_lag_s']:>10.3f}s    | {s['other_max_lag_s']:>10.3f}s")

    # Cleanup flood deltas cleanly
    print("\n3. Compensating flood rows...")
    f_cur.execute("""
        INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
        SELECT 'order', 1, %s::timestamp, 'delivered', -1, -10.00
        FROM generate_series(1, %s)
    """, (fairness_hour, flood_count))
    wait_for_worker(profile, tenant_id=flood_tid, timeout=15.0)

    f_cur.close()
    f_conn.close()
    r_cur.close()
    r_conn.close()
    return drain_time, lag_log

def main():
    profile = "10m"
    print("=" * 70)
    print("RUNNING 10M BENCHMARK & EVALUATION SUITE")
    print("=" * 70)

    # 1. Integrity Tripwire
    verify_integrity_tripwire(profile, fail_fast=True)

    # 2. Restart API and Worker cleanly for 10M
    try:
        subprocess.run(["powershell", "-Command", "Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*uvicorn*' } | Stop-Process -Force"], timeout=5)
    except Exception:
        pass
    time.sleep(1.0)

    print("\nStarting FastAPI and Worker daemon for profile 10M...")
    p_api = subprocess.Popen(["py", "-3.12", "-m", "uvicorn", "api.main:app", "--port", "8000"], env=dict(os.environ, PROFILE=profile))
    p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile, "--poll-interval", "0.5"])

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
        # 3. Query Speed Benchmark (10 tenants, 30 timed iterations)
        print("\n[1] Running Query Speed Benchmark on 10M Profile (10 tenants incl. biggest)...")
        query_results = benchmark_queries(profile=profile, base_url="http://127.0.0.1:8000")

        # 4. Freshness Benchmark (50 probes)
        print("\n[2] Running Freshness Benchmark on 10M Profile (50 sequential probes)...")
        freshness_results = benchmark_freshness(profile=profile, base_url="http://127.0.0.1:8000", count=50)

        # 5. Worker Cycle Time at 100 Tenants
        print("\n[3] Measuring Worker Cycle Time at 100 Tenants on 10M...")
        w_bench = MultiTenantWorker(profile=profile, poll_interval=0.0)
        cycle_times = []
        for _ in range(30):
            res = w_bench.run_one_cycle()
            cycle_times.append(res["duration"] * 1000.0)
        w_bench.close()
        c_p50 = float(np.percentile(cycle_times, 50))
        c_p95 = float(np.percentile(cycle_times, 95))
        c_mean = float(np.mean(cycle_times))
        print(f"  Worker 100-Tenant Cycle Time: mean={c_mean:.2f}ms | p50={c_p50:.2f}ms | p95={c_p95:.2f}ms")

        # 6. EXPLAIN (ANALYZE, BUFFERS) for Top-N Query on Tenant 1
        print("\n[4] EXPLAIN (ANALYZE, BUFFERS) for Top-N query on Tenant 1 (30-day range):")
        explain_output = explain_analyze_top_n(profile=profile, tenant_id=1)
        print(explain_output)

        # 7. Growth Ratio Analysis (1M vs 10M)
        print("\n" + "=" * 90)
        print("GROWTH RATIO (1M vs 10M) PER ENDPOINT:")
        print("=" * 90)
        header = f"{'Endpoint':<25} | {'1M p50':<9} | {'10M p50':<9} | {'p50 Growth':<12} | {'1M p95':<9} | {'10M p95':<9} | {'p95 Growth':<12} | {'Status'}"
        print(header)
        print("-" * len(header))

        ep_mapping = {
            "/orders/count": "count",
            "/orders/trend (hour)": "trend_hour",
            "/orders/by-status": "by_status",
            "/products/top (sales)": "top_products",
        }

        needs_covering_index = False
        for m_name, ep_key in ep_mapping.items():
            ref = REFERENCE_1M[m_name]
            meas = query_results[ep_key]
            p50_ratio = meas["p50"] / ref["p50"]
            p95_ratio = meas["p95"] / ref["p95"]
            status = "PASS (<1.5x)" if p95_ratio <= 1.50 else ("PASS (<2x)" if p95_ratio <= 2.0 else "INVESTIGATE")
            if "products/top" in m_name and p95_ratio > 1.50:
                needs_covering_index = True
            print(f"{m_name:<25} | {ref['p50']:>6.2f} ms | {meas['p50']:>6.2f} ms | {p50_ratio:>9.2f}x    | {ref['p95']:>6.2f} ms | {meas['p95']:>6.2f} ms | {p95_ratio:>9.2f}x    | {status}")

        # 8. Covering Index evaluation if needed
        if needs_covering_index:
            print("\nTop-N query p95 growth exceeded 1.5x. Testing covering index on rollup_product_day...")
            cfg = get_config(profile)
            r_conn = psycopg2.connect(cfg["database"]["rollup_db_url"])
            r_conn.autocommit = True
            r_cur = r_conn.cursor()
            print("Creating covering index: idx_rollup_prod_covering ON rollup_product_day (tenant_id, day) INCLUDE (product_id, units, sales)...")
            r_cur.execute("CREATE INDEX IF NOT EXISTS idx_rollup_prod_covering ON rollup_product_day (tenant_id, day) INCLUDE (product_id, units, sales);")
            r_cur.execute("ANALYZE rollup_product_day;")
            print("Re-evaluating Top-N query after covering index...")
            post_query = benchmark_queries(profile=profile, base_url="http://127.0.0.1:8000")
            meas_post = post_query["top_products"]
            print(f"Top-N after covering index: p50={meas_post['p50']:.2f}ms, p95={meas_post['p95']:.2f}ms (was {meas['p95']:.2f}ms)")
            r_cur.close()
            r_conn.close()

        # 9. Flood biggest tenant at 10M
        drain_s, _ = run_flood_test_10m(profile=profile, flood_count=5000)

    finally:
        if p_worker and p_worker.poll() is None:
            p_worker.terminate()
            p_worker.wait()
        if p_api and p_api.poll() is None:
            p_api.terminate()
            p_api.wait()
        client.close()

    # Final Tripwire
    verify_integrity_tripwire(profile, fail_fast=True)
    print("\n>>> 10M BENCHMARK SUITE FINISHED SUCCESSFULLY <<<")

if __name__ == "__main__":
    main()
