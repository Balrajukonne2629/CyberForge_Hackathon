#!/usr/bin/env python3
"""bench/dashboard_stress.py:
Simulate concurrent dashboard clients hitting the API at 20 req/s per client.
"""
import sys, time, random, threading, argparse
import httpx

def client_worker(stop_evt, base_url, target_rps: float = 20.0):
    client = httpx.Client(base_url=base_url, timeout=5.0)
    tenants = [1, 2, 5, 10, 25, 42, 50, 77, 90, 99]
    interval = 1.0 / target_rps if target_rps > 0 else 0.05
    while not stop_evt.is_set():
        t0 = time.perf_counter()
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
        elapsed = time.perf_counter() - t0
        sleep_dur = max(0.0, interval - elapsed)
        time.sleep(sleep_dur)
    client.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=8, help="Number of concurrent clients")
    parser.add_argument("--rps", type=float, default=20.0, help="Requests per second per client")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="API base URL")
    args = parser.parse_args()

    stop_evt = threading.Event()
    threads = []
    for _ in range(args.clients):
        t = threading.Thread(target=client_worker, args=(stop_evt, args.url, args.rps))
        t.start()
        threads.append(t)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_evt.set()
        for t in threads:
            t.join()
