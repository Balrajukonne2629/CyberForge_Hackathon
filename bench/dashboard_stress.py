#!/usr/bin/env python3
"""Simulate 8 concurrent dashboard clients in a separate process."""
import sys, time, random, threading
import httpx

def client_worker(stop_evt, base_url):
    client = httpx.Client(base_url=base_url, timeout=5.0)
    tenants = [1, 2, 5, 10, 25, 42, 50, 77, 90, 99]
    while not stop_evt.is_set():
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
        time.sleep(0.05)
    client.close()

if __name__ == "__main__":
    stop_evt = threading.Event()
    threads = []
    for _ in range(8):
        t = threading.Thread(target=client_worker, args=(stop_evt, "http://127.0.0.1:8000"))
        t.start()
        threads.append(t)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_evt.set()
        for t in threads:
            t.join()
