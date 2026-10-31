#!/usr/bin/env python3
"""bench/db_safety_dose_response.py:
Dose-Response Curve for Dashboard Load on Main DB:
Measures:
  - Dashboard Clients: 0, 1, 2, 8, 32 (each at 20 req/s against FastAPI)
  - Host CPU %
  - Per-container CPU cores / % (main-db, rollup-db)
  - Server-side OLTP mean execution time via pg_stat_statements
  - Client OLTP p50, p95 (ms)
  - Write throughput (writes/sec)
Evaluates linearity and saturation point.
"""
import sys, os, time, datetime, random, threading, subprocess, statistics
import numpy as np
import httpx, psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.integrity_tripwire import verify_integrity_tripwire

def get_host_cpu():
    try:
        res = subprocess.run(["powershell", "-Command", "(Get-CimInstance Win32_Processor).LoadPercentage"], capture_output=True, text=True, timeout=5)
        return float(res.stdout.strip())
    except Exception:
        return 0.0

def get_container_cpus():
    try:
        res = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}"], capture_output=True, text=True, timeout=5)
        lines = res.stdout.strip().splitlines()
        cpus = {}
        for l in lines:
            parts = l.split("\t")
            if len(parts) == 2:
                name, perc = parts[0].strip(), parts[1].strip().replace("%", "")
                cpus[name] = float(perc)
        return cpus
    except Exception:
        return {}

def setup_pg_stat_statements(profile: str = "1m"):
    for tid in [1]:
        try:
            url = get_tenant_db_url(tid, profile)
            conn = psycopg2.connect(url)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements;")
            cur.execute("SELECT pg_stat_statements_reset();")
            cur.close()
            conn.close()
        except Exception:
            pass

def get_pg_stat_metrics(profile: str = "1m"):
    try:
        url = get_tenant_db_url(1, profile)
        conn = psycopg2.connect(url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                coalesce(sum(calls), 0),
                coalesce(sum(total_exec_time), 0.0)
            FROM pg_stat_statements
            WHERE query LIKE '%orders%' AND query NOT LIKE '%pg_stat_statements%';
        """)
        calls, total_time = cur.fetchone()
        mean_time = (float(total_time) / float(calls)) if calls and calls > 0 else 0.0
        cur.close()
        conn.close()
        return float(mean_time), int(calls)
    except Exception:
        return 0.0, 0

def run_dose_step(clients: int, profile: str = "1m", duration: float = 6.0):
    setup_pg_stat_statements(profile)

    p_worker = subprocess.Popen([sys.executable, "worker/worker.py", "--profile", profile, "--poll-interval", "0.5"])
    time.sleep(1.0)

    p_dash = None
    if clients > 0:
        p_dash = subprocess.Popen([sys.executable, "bench/dashboard_stress.py", "--clients", str(clients), "--rps", "20.0"])
        time.sleep(1.0)

    oltp_lats = []
    write_cnt = [0]
    stop_event = threading.Event()

    def oltp_worker():
        tid = 1
        url = get_tenant_db_url(tid, profile)
        conn = psycopg2.connect(url)
        conn.autocommit = True
        cur = conn.cursor()
        rng = random.Random(42 + clients)
        while not stop_event.is_set():
            oid = rng.randint(1, 100000)
            t0 = time.perf_counter()
            cur.execute("""
                SELECT o.order_id, o.status, o.total, i.product_id, i.qty, i.price
                FROM orders o
                JOIN order_items i ON o.order_id = i.order_id
                WHERE o.order_id = %s
            """, (oid,))
            cur.fetchall()
            oltp_lats.append((time.perf_counter() - t0) * 1000.0)
        cur.close()
        conn.close()

    base_write_id = 920_000_000 + clients * 100_000
    cleaned_ids = []

    def write_worker():
        tid = 1
        url = get_tenant_db_url(tid, profile)
        conn = psycopg2.connect(url)
        conn.autocommit = True
        cur = conn.cursor()
        curr_oid = base_write_id
        while not stop_event.is_set():
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
                write_cnt[0] += 1
                cleaned_ids.append(curr_oid)
            except Exception:
                pass
        cur.close()
        conn.close()

    t_oltp = threading.Thread(target=oltp_worker)
    t_write = threading.Thread(target=write_worker)

    t0 = time.perf_counter()
    t_oltp.start()
    t_write.start()

    # Sample system metrics mid-run
    time.sleep(duration / 2.0)
    host_cpu = get_host_cpu()
    container_cpus = get_container_cpus()
    time.sleep(duration / 2.0)

    stop_event.set()
    t_oltp.join()
    t_write.join()
    act_dur = time.perf_counter() - t0

    server_mean, _ = get_pg_stat_metrics(profile)

    if cleaned_ids:
        try:
            url = get_tenant_db_url(1, profile)
            conn = psycopg2.connect(url)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM order_items WHERE order_id >= %s AND order_id <= %s", (min(cleaned_ids), max(cleaned_ids)))
            cur.execute("DELETE FROM orders WHERE order_id >= %s AND order_id <= %s", (min(cleaned_ids), max(cleaned_ids)))
            cur.close()
            conn.close()
        except Exception:
            pass

    if p_dash and p_dash.poll() is None:
        p_dash.terminate()
        p_dash.wait()
    if p_worker and p_worker.poll() is None:
        p_worker.terminate()
        p_worker.wait()

    arr = np.array(oltp_lats) if oltp_lats else np.array([0.0])
    return {
        "clients": clients,
        "host_cpu": host_cpu,
        "main_db_cpu": container_cpus.get("cyberforge-main-db", 0.0),
        "rollup_db_cpu": container_cpus.get("cyberforge-rollup-db", 0.0),
        "server_mean": server_mean,
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "writes_sec": float(write_cnt[0]) / act_dur
    }

def main():
    profile = "1m"
    verify_integrity_tripwire(profile, fail_fast=True)

    print("Starting FastAPI server for dose-response test...")
    p_api = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--port", "8000"], env=dict(os.environ, PROFILE=profile))
    time.sleep(2.0)

    client_levels = [0, 1, 2, 8, 32]
    results = []

    try:
        for c in client_levels:
            print(f"Testing Dashboard Load: {c:2d} clients @ 20 req/s ({c * 20} req/s)...", flush=True)
            res = run_dose_step(c, profile=profile, duration=5.0)
            results.append(res)
            print(f"  -> Host CPU: {res['host_cpu']:.1f}% | Main-DB: {res['main_db_cpu']:.1f}% | Rollup-DB: {res['rollup_db_cpu']:.1f}% | Server Mean: {res['server_mean']:.2f}ms | Writes/s: {res['writes_sec']:.1f}")
            time.sleep(1.0)
    finally:
        if p_api and p_api.poll() is None:
            p_api.terminate()
            p_api.wait()
        verify_integrity_tripwire(profile, fail_fast=True)

    print("\n" + "=" * 110)
    print("DOSE-RESPONSE CURVE TABLE (DASHBOARD LOAD VS MAIN-DB OLTP HEALTH):")
    print("=" * 110)
    header = f"{'Clients':<8} | {'Req/s':<8} | {'Host CPU%':<10} | {'Main-DB CPU%':<14} | {'Rollup-DB CPU%':<16} | {'Server Mean (ms)':<17} | {'Client p50':<12} | {'Client p95':<12} | {'Writes/sec':<10}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(f"{r['clients']:<8} | {r['clients']*20:<8} | {r['host_cpu']:<10.1f} | {r['main_db_cpu']:<14.1f} | {r['rollup_db_cpu']:<16.1f} | {r['server_mean']:<17.2f} | {r['p50']:<12.2f} | {r['p95']:<12.2f} | {r['writes_sec']:<10.1f}")

if __name__ == "__main__":
    main()
