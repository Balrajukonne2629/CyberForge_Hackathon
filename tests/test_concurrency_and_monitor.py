#!/usr/bin/env python3
"""tests/test_concurrency_and_monitor.py:
Tests Phase 3 Bonuses:
- B-3: Per-tenant concurrency limits (429 with Retry-After on flood; isolation of other tenants <100ms).
- B-5: Admin telemetry monitoring endpoint (/admin/monitor).
- B-1 / B-2: as_of / lag_seconds verification and single-tenant rebuild.
"""
import sys, os, time, asyncio, httpx, numpy as np
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def get_api_url() -> str:
    return f"http://127.0.0.1:{os.getenv('API_PORT', '8000')}"

cfg = get_config("1m")
ADMIN_KEY = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")

async def test_b3_concurrency_throttle():
    api_url = get_api_url()
    print("=" * 70)
    print("1. TESTING B-3: PER-TENANT CONCURRENCY LIMITS & FLOOD ISOLATION")
    print("=" * 70)

    t1_headers = {"X-Tenant-Key": "tenant-key-1"}
    t2_headers = {"X-Tenant-Key": "tenant-key-2"}
    target_url = "/tenants/{}/orders/count?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z"

    # Pre-warm server connection and endpoint query
    async with httpx.AsyncClient(base_url=api_url, timeout=5.0) as client:
        r = await client.get("/health")
        assert r.status_code == 200
        r = await client.get(target_url.format(2), headers=t2_headers)
        assert r.status_code == 200

    # Launch 50 concurrent requests for Tenant 1 and observer requests for Tenant 2 simultaneously
    t1_results = []
    t2_results = []

    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=api_url, limits=limits, timeout=10.0) as flood_client, \
               httpx.AsyncClient(base_url=api_url, limits=limits, timeout=10.0) as t2_client:

        # Pre-warm connection for observer tenant
        await t2_client.get(target_url.format(2), headers=t2_headers)

        async def req_t1():
            try:
                t0 = time.perf_counter()
                res = await flood_client.get(target_url.format(1), headers=t1_headers)
                dur = (time.perf_counter() - t0) * 1000.0
                t1_results.append((res.status_code, dur, res.headers.get("retry-after"), res.text))
            except Exception as e:
                t1_results.append((500, 0.0, None, str(e)))

        # Send observer requests while flood is actively hitting Tenant 1
        async def observer_t2():
            await asyncio.sleep(0.005) # ensure flood is in-flight
            for _ in range(50):
                t0 = time.perf_counter()
                res = await t2_client.get(target_url.format(2), headers=t2_headers)
                dur = (time.perf_counter() - t0) * 1000.0
                t2_results.append((res.status_code, dur, res.headers.get("retry-after"), res.text))
                await asyncio.sleep(0.002)

        tasks = [req_t1() for _ in range(50)] + [observer_t2()]
        await asyncio.gather(*tasks)

    # Analyze Tenant 1 results
    t1_200 = [r for r in t1_results if r[0] == 200]
    t1_429 = [r for r in t1_results if r[0] == 429]
    print(f"Tenant 1 (Flooded with 50 requests):")
    print(f"  - 200 OK responses : {len(t1_200)}/50")
    print(f"  - 429 Throttled    : {len(t1_429)}/50")

    queue_timeout = float(cfg.get("concurrency", {}).get("queue_timeout_seconds", 0.0))
    if queue_timeout <= 0.0:
        assert len(t1_429) > 0, f"Expected some 429s for flooded Tenant 1, got {len(t1_429)}"
    # Verify Retry-After header
    for r in t1_429:
        assert r[2] == "1", f"Expected Retry-After: 1, got {r[2]}"
    if len(t1_429) > 0:
        print("  - Retry-After header: '1' verified on all 429 responses")

    # Analyze Tenant 2 results
    t2_200 = [r for r in t2_results if r[0] == 200]
    t2_429 = [r for r in t2_results if r[0] == 429]
    t2_lats = [r[1] for r in t2_200]
    print(f"  - Tenant 2 latencies: {[round(l, 2) for l in t2_lats]}")
    p50_t2 = float(np.percentile(t2_lats, 50))
    p95_t2 = float(np.percentile(t2_lats, 95))

    print(f"\nTenant 2 (Concurrent Observer during Tenant 1 Flood):")
    print(f"  - 200 OK responses : {len(t2_200)}/{len(t2_results)}")
    print(f"  - 429 Throttled    : {len(t2_429)}/{len(t2_results)} (Expected: 0)")
    print(f"  - Latency p50      : {p50_t2:.2f} ms")
    print(f"  - Latency p95      : {p95_t2:.2f} ms")

    assert len(t2_429) == 0, f"Tenant 2 should not be throttled by Tenant 1 flood, got {len(t2_429)} 429s"
    assert p95_t2 < 100.0, f"Tenant 2 p95 should stay under 100ms, got {p95_t2:.2f}ms"
    print(">>> B-3 CONCURRENCY THROTTLE & ISOLATION PASSED! <<<")

async def test_b5_admin_monitor():
    api_url = get_api_url()
    print("\n" + "=" * 70)
    print("2. TESTING B-5: ADMIN MONITOR TELEMETRY ENDPOINT (/admin/monitor)")
    print("=" * 70)

    async with httpx.AsyncClient(base_url=api_url, timeout=10.0) as client:
        # 1. Auth tests
        r = await client.get("/admin/monitor")
        assert r.status_code == 403, f"Expected 403 for missing key, got {r.status_code}"
        print("  Missing X-Admin-Key: 403 Forbidden (Verified)")

        r = await client.get("/admin/monitor", headers={"X-Admin-Key": "wrong-key"})
        assert r.status_code == 403, f"Expected 403 for wrong key, got {r.status_code}"
        print("  Invalid X-Admin-Key: 403 Forbidden (Verified)")

        # 2. Valid request
        r = await client.get("/admin/monitor", headers={"X-Admin-Key": ADMIN_KEY})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        data = r.json()

        assert "tenants" in data and len(data["tenants"]) == 100
        print(f"  Tenants reported: {len(data['tenants'])} tenants verified")
        t1 = data["tenants"][0]
        assert "tenant_id" in t1 and "lag_seconds" in t1 and "stalled" in t1 and "cursor_xid" in t1 and "quarantine_count" in t1
        print("  Tenant state schema verified:", list(t1.keys()))

        assert "endpoints" in data
        print("  Endpoints 60s ring buffer metrics:")
        for ep, ep_data in data["endpoints"].items():
            print(f"    {ep:<20}: p50={ep_data['p50_ms']}ms, p95={ep_data['p95_ms']}ms, calls={ep_data['calls_60s']}")

        assert "worker" in data
        w = data["worker"]
        print(f"  Worker telemetry: mode={w['mode']}, uptime={w['uptime_seconds']}s, loop_time={w['loop_time_ms']}ms")

        assert "requests" in data
        reqs = data["requests"]
        print(f"  Requests counters: total={reqs['total_requests']}, 429_throttled={reqs['count_429']}")
        assert reqs["total_requests"] > 0
        assert "count_429" in reqs
        print(">>> B-5 ADMIN MONITOR ENDPOINT PASSED! <<<")

async def test_b1_b2_rebuild_and_metadata():
    api_url = get_api_url()
    print("\n" + "=" * 70)
    print("3. TESTING B-1 & B-2: AS_OF / LAG_SECONDS & SINGLE-TENANT REBUILD")
    print("=" * 70)

    async with httpx.AsyncClient(base_url=api_url, timeout=30.0) as client:
        # Check all endpoints return as_of and lag_seconds
        endpoints = [
            "/tenants/1/orders/count?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z",
            "/tenants/1/orders/trend?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z&grain=hour",
            "/tenants/1/orders/by-status?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z",
            "/tenants/1/products/top?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z&n=5",
        ]
        for ep in endpoints:
            r = await client.get(ep, headers={"X-Tenant-Key": "tenant-key-1"})
            assert r.status_code == 200
            res = r.json()
            assert "as_of" in res, f"as_of missing in {ep}"
            assert "lag_seconds" in res, f"lag_seconds missing in {ep}"
            assert "worker_stalled" in res, f"worker_stalled missing in {ep}"
        print("  as_of, lag_seconds, and worker_stalled present on all responses (Verified)")

        # Test single-tenant rebuild
        print("  Executing single-tenant rebuild for Tenant 1...")
        r = await client.post("/admin/rebuild?tenant_id=1", headers={"X-Admin-Key": ADMIN_KEY})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        res = r.json()
        assert res["status"] == "success" and res["scope"] == "tenant_1"
        print("  POST /admin/rebuild?tenant_id=1: SUCCESS (Verified)")

    print(">>> B-1 & B-2 VERIFICATION PASSED! <<<")

async def main():
    await test_b3_concurrency_throttle()
    await test_b5_admin_monitor()
    await test_b1_b2_rebuild_and_metadata()

def run_concurrency_and_monitor_tests() -> bool:
    asyncio.run(main())
    return True

if __name__ == "__main__":
    asyncio.run(main())
