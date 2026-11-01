#!/usr/bin/env python3
"""tests/test_b3_policy.py:
Tests B-3 Concurrency Policy:
  a) 8 clients on one tenant for 10s: must be zero 429.
  b) 64 clients, then 200 clients on one tenant for 10s: paste 200/429 counts and p95.
  c) During (b), observer tenant (Tenant 2) p95 must stay normal.
"""
import sys, os, time, asyncio, httpx, subprocess, socket, psycopg2, numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

cfg = get_config("1m")
ADMIN_KEY = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")
ROLLUP_URL = cfg["database"]["rollup_db_url"]
MAIN_URL = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

def get_session_counts():
    c1 = psycopg2.connect(MAIN_URL)
    cur1 = c1.cursor()
    cur1.execute("SELECT count(*) FROM pg_stat_activity")
    cnt1 = cur1.fetchone()[0]
    cur1.close()
    c1.close()

    c2 = psycopg2.connect(ROLLUP_URL)
    cur2 = c2.cursor()
    cur2.execute("SELECT count(*) FROM pg_stat_activity")
    cnt2 = cur2.fetchone()[0]
    cur2.close()
    c2.close()
    return cnt1, cnt2

def find_free_port(preferred: int = 8000) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

async def run_client_loop(client: httpx.AsyncClient, url: str, headers: dict, stop_time: float, results: list):
    while time.time() < stop_time:
        t0 = time.perf_counter()
        try:
            res = await client.get(url, headers=headers)
            dur = (time.perf_counter() - t0) * 1000.0
            results.append((res.status_code, dur, res.headers.get("retry-after")))
        except Exception as e:
            dur = (time.perf_counter() - t0) * 1000.0
            results.append((500, dur, None))

async def run_observer_loop(client: httpx.AsyncClient, url: str, headers: dict, stop_time: float, results: list):
    while time.time() < stop_time:
        t0 = time.perf_counter()
        try:
            res = await client.get(url, headers=headers)
            dur = (time.perf_counter() - t0) * 1000.0
            results.append((res.status_code, dur))
        except Exception:
            dur = (time.perf_counter() - t0) * 1000.0
            results.append((500, dur))
        await asyncio.sleep(0.02)

async def main():
    port = find_free_port(8000)
    base_url = f"http://127.0.0.1:{port}"
    print(f"Starting API server on port {port}...")
    p_api = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--port", str(port), "--no-access-log"], env=dict(os.environ, PROFILE="1m"))
    
    # Wait for API ready
    t0 = time.time()
    while time.time() - t0 < 10.0:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)

    try:
        t1_url = "/tenants/1/orders/count?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z"
        t2_url = "/tenants/2/orders/count?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z"
        t1_headers = {"X-Tenant-Key": "tenant-key-1"}
        t2_headers = {"X-Tenant-Key": "tenant-key-2"}

        limits = httpx.Limits(max_connections=300, max_keepalive_connections=300)

        # Print pg_stat_activity count right before first timing test
        cnt1, cnt2 = get_session_counts()
        print(f"\n[PG_STAT_ACTIVITY RIGHT BEFORE FIRST TIMING TEST] 5432 (tenants): {cnt1} sessions | 5433 (rollup): {cnt2} sessions\n")

        # =====================================================================
        # Test 1(a): 8 clients on Tenant 1 for 10 s
        # =====================================================================
        print("=" * 70)
        print("TEST 1(a): 8 clients on Tenant 1 for 10s (limit=32, queue_timeout=1.0s)")
        print("=" * 70)
        results_8 = []
        async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=10.0) as client:
            # Pre-warm
            await client.get("/health")
            await client.get(t1_url, headers=t1_headers)

            stop_time = time.time() + 10.0
            tasks = [run_client_loop(client, t1_url, t1_headers, stop_time, results_8) for _ in range(8)]
            await asyncio.gather(*tasks)

        t1_8_200 = [r for r in results_8 if r[0] == 200]
        t1_8_429 = [r for r in results_8 if r[0] == 429]
        t1_8_lats = [r[1] for r in t1_8_200]
        p95_8 = float(np.percentile(t1_8_lats, 95)) if t1_8_lats else 0.0

        print(f"Results for 8 clients (10s):")
        print(f"  - Total requests : {len(results_8)}")
        print(f"  - 200 OK counts  : {len(t1_8_200)}")
        print(f"  - 429 Throttled  : {len(t1_8_429)}")
        print(f"  - p95 Latency    : {p95_8:.2f} ms")
        assert len(t1_8_429) == 0, f"Expected zero 429 for 8 clients, got {len(t1_8_429)}"

        await asyncio.sleep(1.0)

        # =====================================================================
        # Test 1(b) Part 1: 64 clients on Tenant 1 for 10s + Observer Tenant 2
        # =====================================================================
        print("\n" + "=" * 70)
        print("TEST 1(b) Part 1: 64 clients on Tenant 1 for 10s + Observer Tenant 2")
        print("=" * 70)
        results_64 = []
        results_obs_64 = []
        async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=15.0) as client_t1, \
                   httpx.AsyncClient(base_url=base_url, limits=limits, timeout=15.0) as client_t2:
            stop_time = time.time() + 10.0
            tasks = [run_client_loop(client_t1, t1_url, t1_headers, stop_time, results_64) for _ in range(64)]
            tasks.append(run_observer_loop(client_t2, t2_url, t2_headers, stop_time, results_obs_64))
            await asyncio.gather(*tasks)

        t1_64_200 = [r for r in results_64 if r[0] == 200]
        t1_64_429 = [r for r in results_64 if r[0] == 429]
        t1_64_lats = [r[1] for r in t1_64_200]
        p95_64 = float(np.percentile(t1_64_lats, 95)) if t1_64_lats else 0.0

        obs_64_200 = [r for r in results_obs_64 if r[0] == 200]
        obs_64_429 = [r for r in results_obs_64 if r[0] == 429]
        obs_64_lats = [r[1] for r in obs_64_200]
        obs_p95_64 = float(np.percentile(obs_64_lats, 95)) if obs_64_lats else 0.0

        print(f"Results for 64 clients on Tenant 1 (10s):")
        print(f"  - Total requests : {len(results_64)}")
        print(f"  - 200 OK counts  : {len(t1_64_200)}")
        print(f"  - 429 Throttled  : {len(t1_64_429)}")
        print(f"  - Tenant 1 p95   : {p95_64:.2f} ms")
        print(f"Results for Observer Tenant 2 during 64 clients flood:")
        print(f"  - Total requests : {len(results_obs_64)}")
        print(f"  - 200 OK counts  : {len(obs_64_200)}")
        print(f"  - 429 Throttled  : {len(obs_64_429)}")
        print(f"  - Tenant 2 p95   : {obs_p95_64:.2f} ms")

        await asyncio.sleep(1.0)

        # =====================================================================
        # Test 1(b) Part 2: 200 clients on Tenant 1 for 10s + Observer Tenant 2
        # =====================================================================
        print("\n" + "=" * 70)
        print("TEST 1(b) Part 2: 200 clients on Tenant 1 for 10s + Observer Tenant 2")
        print("=" * 70)
        results_200 = []
        results_obs_200 = []
        async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=15.0) as client_t1, \
                   httpx.AsyncClient(base_url=base_url, limits=limits, timeout=15.0) as client_t2:
            stop_time = time.time() + 10.0
            tasks = [run_client_loop(client_t1, t1_url, t1_headers, stop_time, results_200) for _ in range(200)]
            tasks.append(run_observer_loop(client_t2, t2_url, t2_headers, stop_time, results_obs_200))
            await asyncio.gather(*tasks)

        t1_200_200 = [r for r in results_200 if r[0] == 200]
        t1_200_429 = [r for r in results_200 if r[0] == 429]
        t1_200_lats = [r[1] for r in t1_200_200]
        p95_200 = float(np.percentile(t1_200_lats, 95)) if t1_200_lats else 0.0

        obs_200_200 = [r for r in results_obs_200 if r[0] == 200]
        obs_200_429 = [r for r in results_obs_200 if r[0] == 429]
        obs_200_lats = [r[1] for r in obs_200_200]
        obs_p95_200 = float(np.percentile(obs_200_lats, 95)) if obs_200_lats else 0.0

        print(f"Results for 200 clients on Tenant 1 (10s):")
        print(f"  - Total requests : {len(results_200)}")
        print(f"  - 200 OK counts  : {len(t1_200_200)}")
        print(f"  - 429 Throttled  : {len(t1_200_429)}")
        print(f"  - Tenant 1 p95   : {p95_200:.2f} ms")
        print(f"Results for Observer Tenant 2 during 200 clients flood:")
        print(f"  - Total requests : {len(results_obs_200)}")
        print(f"  - 200 OK counts  : {len(obs_200_200)}")
        print(f"  - 429 Throttled  : {len(obs_200_429)}")
        print(f"  - Tenant 2 p95   : {obs_p95_200:.2f} ms")

        # Verify retry-after header on 429s if any
        for r in t1_200_429:
            assert r[2] == "1", f"Expected Retry-After: 1, got {r[2]}"

    finally:
        p_api.terminate()
        p_api.wait()

if __name__ == "__main__":
    asyncio.run(main())
