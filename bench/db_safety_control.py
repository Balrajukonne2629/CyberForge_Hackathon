#!/usr/bin/env python3
"""bench/db_safety_control.py:
Control Experiments to Isolate Main-DB Impact:
Compares:
  - Baseline (c): Triggers ON, Worker ON, 0 dashboard load
  - Dashboard 8 Clients: 160 req/s against Rollup DB
  - Control 1: Plain CPU Burner (burns equivalent host CPU, 0 DB activity)
  - Control 2: Plain Disk-Sync Burner (~100 fsyncs/sec against local disk, 0 DB activity)
Measures OLTP latency and writes/sec to isolate the root mechanism.
"""
import sys, os, time, datetime, random, threading, subprocess, tempfile
import numpy as np
import httpx, psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.integrity_tripwire import verify_integrity_tripwire

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
        return float(mean_time)
    except Exception:
        return 0.0

def cpu_burner(stop_event: threading.Event):
    while not stop_event.is_set():
        _ = [x * x for x in range(10000)]

def disk_sync_burner(stop_event: threading.Event, rate_per_sec: int = 100):
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        interval = 1.0 / rate_per_sec
        buf = b"x" * 512
        while not stop_event.is_set():
            t0 = time.perf_counter()
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, buf)
            os.fsync(fd)
            elapsed = time.perf_counter() - t0
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        os.close(fd)
    finally:
        if os.path.exists(path):
            os.remove(path)

def measure_oltp(profile: str, duration: float = 5.0):
    oltp_lats = []
    write_cnt = [0]
    stop_event = threading.Event()

    def oltp_worker():
        tid = 1
        url = get_tenant_db_url(tid, profile)
        conn = psycopg2.connect(url)
        conn.autocommit = True
        cur = conn.cursor()
        rng = random.Random(42)
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

    base_write_id = 930_000_000 + int(time.time() * 1000) % 10_000_000
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

    time.sleep(duration)

    stop_event.set()
    t_oltp.join()
    t_write.join()
    act_dur = time.perf_counter() - t0

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

    arr = np.array(oltp_lats) if oltp_lats else np.array([0.0])
    return {
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "mean": float(np.mean(arr)),
        "writes_sec": float(write_cnt[0]) / act_dur
    }

def run_control_experiments():
    profile = "1m"
    verify_integrity_tripwire(profile, fail_fast=True)

    print("Starting worker process...")
    p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", "--profile", profile, "--poll-interval", "0.5"])
    time.sleep(1.0)

    try:
        # 1. Baseline (c): Triggers ON, Worker ON, No Load
        print("1. Measuring Baseline (c)...")
        res_baseline = measure_oltp(profile, 5.0)

        # 2. Real Dashboard Load (8 clients @ 20 req/s)
        print("2. Measuring Dashboard Load (8 clients @ 20 req/s)...")
        p_api = subprocess.Popen(["py", "-3.12", "-m", "uvicorn", "api.main:app", "--port", "8000"], env=dict(os.environ, PROFILE=profile))
        time.sleep(1.5)
        p_dash = subprocess.Popen(["py", "-3.12", "bench/dashboard_stress.py", "--clients", "8", "--rps", "20.0"])
        time.sleep(0.5)
        res_dash = measure_oltp(profile, 5.0)
        p_dash.terminate()
        p_dash.wait()
        p_api.terminate()
        p_api.wait()

        # 3. Control 1: Plain CPU Burner (4 burner threads)
        print("3. Measuring Control 1: Host CPU Burner...")
        stop_cpu = threading.Event()
        cpu_threads = [threading.Thread(target=cpu_burner, args=(stop_cpu,)) for _ in range(4)]
        for t in cpu_threads: t.start()
        res_cpu = measure_oltp(profile, 5.0)
        stop_cpu.set()
        for t in cpu_threads: t.join()

        # 4. Control 2: Plain Disk-Sync Burner (~100 fsync/s)
        print("4. Measuring Control 2: Local Disk-Sync Burner (~100 fsync/s)...")
        stop_sync = threading.Event()
        t_sync = threading.Thread(target=disk_sync_burner, args=(stop_sync, 100))
        t_sync.start()
        res_sync = measure_oltp(profile, 5.0)
        stop_sync.set()
        t_sync.join()

        print("\n" + "=" * 90)
        print("CONTROL EXPERIMENT RESULTS (ISOLATING CAUSE OF MAIN-DB SLOWDOWN):")
        print("=" * 90)
        print(f"{'Condition':<35} | {'OLTP p50':<10} | {'OLTP p95':<10} | {'OLTP Mean':<10} | {'Writes/s':<10} | {'p95 Slowdown vs (c)'}")
        print("-" * 90)
        for label, r in [
            ("Baseline (c) Triggers + Worker", res_baseline),
            ("Dashboard Load (8 Clients @ 20 rps)", res_dash),
            ("Control 1: Host CPU Burner", res_cpu),
            ("Control 2: Disk Fsync Burner (100/s)", res_sync)
        ]:
            slowdown = ((r["p95"] - res_baseline["p95"]) / res_baseline["p95"]) * 100.0
            print(f"{label:<35} | {r['p50']:<10.2f} | {r['p95']:<10.2f} | {r['mean']:<10.2f} | {r['writes_sec']:<10.1f} | {slowdown:>+18.1f}%")

    finally:
        if p_worker and p_worker.poll() is None:
            p_worker.terminate()
            p_worker.wait()
        verify_integrity_tripwire(profile, fail_fast=True)

if __name__ == "__main__":
    run_control_experiments()
