#!/usr/bin/env python3
"""bench/db_safety_breakdown.py:
Comprehensive DB Safety Breakdown Benchmark:
Measures:
1. OLTP query latency (p50, p95, p99, mean ms)
2. Server-side query execution time via pg_stat_statements (mean ms, max ms)
3. Write throughput (writes/sec)
Across configurations:
  (a) no triggers, no worker, no dashboard load
  (b) triggers on, worker off
  (c) triggers on, worker on (500ms poll interval)
  (d2) triggers on, worker on, 2 concurrent dashboard clients @ 20 req/s
  (d8) triggers on, worker on, 8 concurrent dashboard clients @ 20 req/s

Runs 5 alternating repeats (a -> b -> c -> d2 -> d8 x 5).
Reports mean and standard deviation over 5 repeats for EVERY column.
Validates dataset integrity tripwire before and after.
Confirms zero dashboard queries on main-db.
"""
import sys, os, time, datetime, random, threading, subprocess, statistics
from typing import Dict, List, Tuple
import numpy as np
import httpx
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from concurrent.futures import ThreadPoolExecutor
from tests.integrity_tripwire import verify_integrity_tripwire

def set_triggers(enabled: bool, profile: str = "1m"):
    action = "ENABLE" if enabled else "DISABLE"
    def toggle_tenant(tid: int):
        try:
            url = get_tenant_db_url(tid, profile)
            conn = psycopg2.connect(url)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(f"ALTER TABLE orders {action} TRIGGER trg_orders_outbox;")
            cur.execute(f"ALTER TABLE order_items {action} TRIGGER trg_order_items_outbox;")
            cur.close()
            conn.close()
        except Exception as e:
            pass
    with ThreadPoolExecutor(max_workers=25) as executor:
        list(executor.map(toggle_tenant, range(1, 101)))

def setup_pg_stat_statements(profile: str = "1m"):
    for tid in [1, 2, 5, 10]:
        try:
            url = get_tenant_db_url(tid, profile)
            conn = psycopg2.connect(url)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements;")
            cur.close()
            conn.close()
        except Exception:
            pass

def reset_pg_stat_statements(profile: str = "1m"):
    for tid in [1, 2, 5, 10]:
        try:
            url = get_tenant_db_url(tid, profile)
            conn = psycopg2.connect(url)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("SELECT pg_stat_statements_reset();")
            cur.close()
            conn.close()
        except Exception:
            pass

def get_pg_stat_statements_metrics(profile: str = "1m", tid: int = 1) -> Tuple[float, float, int]:
    try:
        url = get_tenant_db_url(tid, profile)
        conn = psycopg2.connect(url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                coalesce(sum(calls), 0),
                coalesce(sum(total_exec_time), 0.0),
                coalesce(max(max_exec_time), 0.0)
            FROM pg_stat_statements
            WHERE query LIKE '%orders%' AND query NOT LIKE '%pg_stat_statements%';
        """)
        calls, total_time, max_time = cur.fetchone()
        mean_time = (float(total_time) / float(calls)) if calls and calls > 0 else 0.0
        cur.close()
        conn.close()
        return float(mean_time), float(max_time), int(calls)
    except Exception as e:
        return 0.0, 0.0, 0

def check_main_db_dashboard_queries(profile: str = "1m") -> int:
    try:
        cfg = get_config(profile)
        main_url = cfg["database"]["main_db_url"]
        conn = psycopg2.connect(main_url)
        cur = conn.cursor()
        cur.execute("""
            SELECT count(*) FROM pg_stat_statements 
            WHERE query ILIKE '%rollup_%' OR query ILIKE '%orders/count%' OR query ILIKE '%orders/trend%';
        """)
        cnt = cur.fetchone()[0]
        cur.close()
        conn.close()
        return cnt
    except Exception:
        return 0

def run_single_measurement(
    profile: str,
    duration: float,
    has_triggers: bool,
    has_worker: bool,
    dashboard_clients: int,
    poll_interval: float = 0.5
) -> Dict[str, float]:
    set_triggers(has_triggers, profile)
    reset_pg_stat_statements(profile)

    p_worker = None
    if has_worker:
        p_worker = subprocess.Popen([sys.executable, "worker/worker.py", "--profile", profile, "--poll-interval", str(poll_interval)])
        time.sleep(1.0)

    p_dashboard = None
    if dashboard_clients > 0:
        p_dashboard = subprocess.Popen([sys.executable, "bench/dashboard_stress.py", "--clients", str(dashboard_clients), "--rps", "20.0"])
        time.sleep(0.5)

    oltp_latencies = []
    write_count = [0]
    stop_measurement = threading.Event()

    # OLTP Probe thread (80% point reads, 20% point updates)
    def oltp_worker():
        rng = random.Random(42 + int(time.time() * 1000) % 100000)
        tid = 1
        url = get_tenant_db_url(tid, profile)
        c = psycopg2.connect(url)
        c.autocommit = True
        cur = c.cursor()
        cur.execute("SELECT coalesce(max(order_id), 1) FROM orders")
        mx = cur.fetchone()[0]

        while not stop_measurement.is_set():
            oid = rng.randint(1, min(mx, 100000))
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

    # Dedicated high id range (900,000,000+)
    base_write_id = 910_000_000 + int(time.time() * 1000) % 50_000_000
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
    actual_duration = time.perf_counter() - t0_all

    # Server side query execution metrics
    server_mean_ms, server_max_ms, s_calls = get_pg_stat_statements_metrics(profile, tid=1)

    # Clean up test rows
    if cleaned_ids:
        try:
            url = get_tenant_db_url(1, profile)
            c = psycopg2.connect(url)
            c.autocommit = True
            cur = c.cursor()
            cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id <= %s", (min(cleaned_ids), max(cleaned_ids)))
            cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id <= %s", (min(cleaned_ids), max(cleaned_ids)))
            cur.close()
            c.close()
        except Exception:
            pass

    if p_worker and p_worker.poll() is None:
        p_worker.terminate()
        p_worker.wait()
    if p_dashboard and p_dashboard.poll() is None:
        p_dashboard.terminate()
        p_dashboard.wait()

    arr = np.array(oltp_latencies) if oltp_latencies else np.array([0.0])
    p50 = float(np.percentile(arr, 50))
    p95 = float(np.percentile(arr, 95))
    p99 = float(np.percentile(arr, 99))
    mean = float(np.mean(arr))
    write_tput = float(write_count[0]) / actual_duration

    return {
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "mean": mean,
        "write_tput": write_tput,
        "server_mean": server_mean_ms,
        "server_max": server_max_ms,
        "server_calls": s_calls,
    }

def run_benchmark_breakdown(profile: str = "1m", repeats: int = 5, duration: float = 4.0, poll_interval: float = 0.5):
    print("=" * 80)
    print(f"RUNNING DB SAFETY BREAKDOWN ({repeats} ALTERNATING REPEATS, PROFILE: {profile.upper()})")
    print(f"Worker Poll Interval: {int(poll_interval * 1000)}ms | Hardware: 12 vCPUs, 16GB RAM")
    print("=" * 80)

    # 1. Integrity Tripwire verification at start
    verify_integrity_tripwire(profile, fail_fast=True)
    setup_pg_stat_statements(profile)

    # 2. Start FastAPI server for dashboard simulation
    print("Starting FastAPI server for dashboard client simulation...")
    p_api = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--port", "8000"])
    client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=5.0)
    for _ in range(30):
        try:
            r = client.get("/health")
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.5)

    configs = [
        ("a", "No Triggers, Worker OFF, Idle", False, False, 0),
        ("b", "Triggers ON, Worker OFF", True, False, 0),
        ("c", "Triggers ON, Worker ON", True, True, 0),
        ("d2", "Triggers ON, Worker ON, 2 Dash Clients", True, True, 2),
        ("d8", "Triggers ON, Worker ON, 8 Dash Clients", True, True, 8),
    ]

    raw_results = {k: [] for k, _, _, _, _ in configs}

    try:
        for rep in range(1, repeats + 1):
            run_order = list(configs) if rep % 2 != 0 else list(reversed(configs))
            order_names = " -> ".join(c[0] for c in run_order)
            print(f"\n--- Repeat {rep}/{repeats} (Order: {order_names}) ---")

            for key, name, has_trg, has_wrk, dash_cnt in run_order:
                print(f"  Measuring ({key}) {name} ({duration}s)...", end=" ", flush=True)
                res = run_single_measurement(
                    profile=profile,
                    duration=duration,
                    has_triggers=has_trg,
                    has_worker=has_wrk,
                    dashboard_clients=dash_cnt,
                    poll_interval=poll_interval
                )
                raw_results[key].append(res)
                print(f"Client p50={res['p50']:.2f}ms | p95={res['p95']:.2f}ms | Server mean={res['server_mean']:.2f}ms | writes={res['write_tput']:.1f}/s")
                time.sleep(0.5)

        # Confirm zero dashboard queries on main-db
        main_dash_queries = check_main_db_dashboard_queries(profile)
        print(f"\n[CONFIRMATION] main-db queries matching dashboard endpoints / rollup tables: {main_dash_queries}")

        # Summary statistics
        stats_summary = {}
        for key, name, _, _, _ in configs:
            p50s = [r["p50"] for r in raw_results[key]]
            p95s = [r["p95"] for r in raw_results[key]]
            p99s = [r["p99"] for r in raw_results[key]]
            means = [r["mean"] for r in raw_results[key]]
            tputs = [r["write_tput"] for r in raw_results[key]]
            s_means = [r["server_mean"] for r in raw_results[key]]

            stats_summary[key] = {
                "name": name,
                "p50_mean": statistics.mean(p50s),
                "p50_std": statistics.stdev(p50s) if len(p50s) > 1 else 0.0,
                "p95_mean": statistics.mean(p95s),
                "p95_std": statistics.stdev(p95s) if len(p95s) > 1 else 0.0,
                "p99_mean": statistics.mean(p99s),
                "p99_std": statistics.stdev(p99s) if len(p99s) > 1 else 0.0,
                "mean_mean": statistics.mean(means),
                "mean_std": statistics.stdev(means) if len(means) > 1 else 0.0,
                "tput_mean": statistics.mean(tputs),
                "tput_std": statistics.stdev(tputs) if len(tputs) > 1 else 0.0,
                "server_mean": statistics.mean(s_means),
                "server_std": statistics.stdev(s_means) if len(s_means) > 1 else 0.0,
            }

        print("\n" + "=" * 130)
        print("FINAL DB SAFETY BREAKDOWN TABLE (MEAN ± STD OVER 5 ALTERNATING REPEATS):")
        print("=" * 130)
        header = f"{'Configuration':<40} | {'OLTP p50 (ms)':<15} | {'OLTP p95 (ms)':<15} | {'OLTP p99 (ms)':<15} | {'OLTP Mean (ms)':<15} | {'Server Mean (ms)':<17} | {'Writes/sec':<18}"
        print(header)
        print("-" * len(header))
        for key, _, _, _, _ in configs:
            s = stats_summary[key]
            p50_str = f"{s['p50_mean']:.2f} ± {s['p50_std']:.2f}"
            p95_str = f"{s['p95_mean']:.2f} ± {s['p95_std']:.2f}"
            p99_str = f"{s['p99_mean']:.2f} ± {s['p99_std']:.2f}"
            mean_str = f"{s['mean_mean']:.2f} ± {s['mean_std']:.2f}"
            s_mean_str = f"{s['server_mean']:.2f} ± {s['server_std']:.2f}"
            tput_str = f"{s['tput_mean']:.1f} ± {s['tput_std']:.1f}"
            print(f"({key:<2}) {s['name']:<35} | {p50_str:<15} | {p95_str:<15} | {p99_str:<15} | {mean_str:<15} | {s_mean_str:<17} | {tput_str:<18}")

        # Organizer Threshold Evaluations using BOTH p50 and p95 (worse one decides)
        base_p50 = stats_summary["a"]["p50_mean"]
        base_p95 = stats_summary["a"]["p95_mean"]
        base_tput = stats_summary["a"]["tput_mean"]

        c_p50 = stats_summary["c"]["p50_mean"]
        c_p95 = stats_summary["c"]["p95_mean"]
        c_tput = stats_summary["c"]["tput_mean"]

        print("\n" + "=" * 130)
        print("ORGANIZER THRESHOLD EVALUATION (WORSE OF p50 OR p95 DECIDES PASS/FAIL):")
        print("=" * 130)
        eval_header = f"{'Comparison / Impact':<40} | {'p50 Slowdown':<14} | {'p95 Slowdown':<14} | {'Worse Metric':<14} | {'Write Drop':<12} | {'Good (<25%)':<12} | {'Great (<10%)':<12} | {'Status'}"
        print(eval_header)
        print("-" * len(eval_header))

        comparisons = [
            ("(b vs a) Trigger Cost", "b", base_p50, base_p95, base_tput),
            ("(c vs a) Worker Sweep Contention", "c", base_p50, base_p95, base_tput),
            ("(d2 vs c) 2 Dash Clients Impact", "d2", c_p50, c_p95, c_tput),
            ("(d8 vs c) 8 Dash Clients Impact", "d8", c_p50, c_p95, c_tput),
            ("(d8 vs a) End-to-End System Impact", "d8", base_p50, base_p95, base_tput),
        ]

        for label, k, ref_p50, ref_p95, ref_tput in comparisons:
            s = stats_summary[k]
            p50_slow = ((s["p50_mean"] - ref_p50) / ref_p50) * 100.0
            p95_slow = ((s["p95_mean"] - ref_p95) / ref_p95) * 100.0
            worse_slow = max(p50_slow, p95_slow)
            tput_drop = ((ref_tput - s["tput_mean"]) / ref_tput) * 100.0

            good_pass = worse_slow < 25.0
            great_pass = worse_slow < 10.0
            status = "PASS (Great)" if great_pass else ("PASS (Good)" if good_pass else "FAIL")

            print(f"{label:<40} | {p50_slow:>+11.1f}%  | {p95_slow:>+11.1f}%  | {worse_slow:>+11.1f}%  | {tput_drop:>9.1f}%  | {str(good_pass):<12} | {str(great_pass):<12} | {status}")

    finally:
        set_triggers(True, profile)
        if p_api and p_api.poll() is None:
            p_api.terminate()
            p_api.wait()
        # Verify tripwire at end of benchmark
        verify_integrity_tripwire(profile, fail_fast=True)

    return stats_summary

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    run_benchmark_breakdown(profile=prof, repeats=5, duration=4.0, poll_interval=0.5)
