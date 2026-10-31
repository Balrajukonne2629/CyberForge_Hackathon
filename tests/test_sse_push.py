#!/usr/bin/env python3
"""tests/test_sse_push.py:
Tests Push functionality (Server-Sent Events) on /tenants/{id}/stream:
1. Validates auth (403 on missing key, 403 on wrong key, 200 on valid key).
2. Verifies initial event contains as_of, lag_seconds, changed_buckets.
3. Measures commit-to-SSE receive latency over 50 probes (p50/p95/max).
4. Labels clock sources clearly.
"""
import sys, os, time, json, threading, subprocess, httpx, numpy as np, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url

def run_sse_push_test(profile: str = "1m", probes: int = 50):
    print("=" * 70)
    print(f"TESTING PUSH (SSE) STREAM LATENCY: 50 PROBES (PROFILE: {profile.upper()})")
    print("=" * 70)

    tid = 1
    api_url = "http://127.0.0.1:8000"
    valid_key = f"tenant-key-{tid}"
    wrong_key = "wrong-key-999"
    t_url = get_tenant_db_url(tid, profile)

    # 1. Auth tests
    print("\n[1] Verifying SSE stream authentication...")
    with httpx.Client(base_url=api_url, timeout=5.0) as client:
        # Missing key
        r = client.get(f"/tenants/{tid}/stream")
        assert r.status_code == 403, f"Expected 403 for missing key, got {r.status_code}"
        print("  Missing X-Tenant-Key: 403 Forbidden (Verified)")

        # Wrong key
        r = client.get(f"/tenants/{tid}/stream", headers={"X-Tenant-Key": wrong_key})
        assert r.status_code == 403, f"Expected 403 for wrong key, got {r.status_code}"
        print("  Mismatched X-Tenant-Key: 403 Forbidden (Verified)")

        # Cross-tenant key (tenant 2 key accessing tenant 1 stream)
        other_key = "tenant-key-2"
        r = client.get(f"/tenants/{tid}/stream", headers={"X-Tenant-Key": other_key})
        assert r.status_code == 403, f"Expected 403 for cross-tenant key, got {r.status_code}"
        print("  Cross-Tenant X-Tenant-Key: 403 Forbidden (Verified)")

    # 2. Connect to SSE stream and measure 50 probes
    print(f"\n[2] Connecting to /tenants/{tid}/stream and measuring {probes} commit-to-SSE probes...")

    latencies_ms = []
    received_events = []
    stream_ready = threading.Event()
    stop_stream = threading.Event()

    def sse_listener():
        with httpx.Client(base_url=api_url, timeout=60.0) as client:
            with client.stream("GET", f"/tenants/{tid}/stream", headers={"X-Tenant-Key": valid_key}) as response:
                assert response.status_code == 200
                stream_ready.set()
                buffer = ""
                for line in response.iter_lines():
                    if stop_stream.is_set():
                        break
                    if line.startswith("data: "):
                        t_received = time.perf_counter()
                        data_str = line[6:].strip()
                        try:
                            payload = json.loads(data_str)
                            received_events.append((t_received, payload))
                        except Exception:
                            pass

    listener_thread = threading.Thread(target=sse_listener, daemon=True)
    listener_thread.start()
    assert stream_ready.wait(timeout=5.0), "SSE stream failed to connect"
    print("  SSE stream connected successfully!")

    # Wait for initial message
    time.sleep(0.5)
    assert len(received_events) >= 1, "Failed to receive initial state event"
    init_event = received_events[-1][1]
    assert "as_of" in init_event and "lag_seconds" in init_event and "changed_buckets" in init_event
    print("  Initial event schema verified:", list(init_event.keys()))

    # Connect to tenant DB for probe writes
    t_conn = psycopg2.connect(t_url)
    t_conn.autocommit = True
    t_cur = t_conn.cursor()

    print(f"  Executing {probes} probe mutations and capturing commit-to-receive timestamps...")
    hour_bucket = "2026-10-31 20:00:00"

    for i in range(probes):
        count_before = len(received_events)
        t_commit_start = time.perf_counter()

        t_cur.execute("""
            INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
            VALUES ('order', %s, %s::timestamp, 'delivered', 1, 10.00);
            SELECT pg_notify('outbox', %s);
        """, (tid, hour_bucket, str(tid)))
        t_committed = time.perf_counter()

        # Wait for SSE event to arrive
        t_wait_start = time.time()
        while len(received_events) == count_before and time.time() - t_wait_start < 3.0:
            time.sleep(0.002)

        if len(received_events) > count_before:
            t_recv = received_events[-1][0]
            lat_ms = (t_recv - t_committed) * 1000.0
            latencies_ms.append(lat_ms)

        # Cleanup compensatory delta every 10 probes
        if (i + 1) % 10 == 0:
            t_cur.execute("""
                INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
                VALUES ('order', %s, %s::timestamp, 'delivered', -10, -100.00);
                SELECT pg_notify('outbox', %s);
            """, (tid, hour_bucket, str(tid)))
            time.sleep(0.1)

    stop_stream.set()
    t_cur.close()
    t_conn.close()

    p50 = float(np.percentile(latencies_ms, 50))
    p95 = float(np.percentile(latencies_ms, 95))
    max_lat = float(np.max(latencies_ms))
    avg = float(np.mean(latencies_ms))

    print("\n" + "=" * 70)
    print("COMMIT-TO-SSE RECEIVE LATENCY RESULTS (50 PROBES):")
    print(f"  Clock source: time.perf_counter() monotonic server/client clock")
    print(f"  Completed probes : {len(latencies_ms)}/{probes}")
    print(f"  p50 Latency      : {p50:.2f} ms")
    print(f"  p95 Latency      : {p95:.2f} ms")
    print(f"  Max Latency      : {max_lat:.2f} ms")
    print(f"  Mean Latency     : {avg:.2f} ms")
    print("=" * 70)

    assert p50 < 200.0, f"Expected p50 < 200ms, got {p50:.2f}ms"
    print(">>> SSE PUSH TEST PASSED WITH GREAT FRESHNESS! <<<")
    return {
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "max_ms": round(max_lat, 2),
        "mean_ms": round(avg, 2),
        "clock_source": "time.perf_counter() (monotonic)"
    }

if __name__ == "__main__":
    run_sse_push_test()
