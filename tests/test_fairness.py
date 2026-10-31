#!/usr/bin/env python3
"""tests/test_fairness.py:
Tests anti-starvation & fairness across tenants:
1. Floods Tenant 1 with 1,000 outbox deltas in a single burst.
2. Simultaneously injects orders into observer tenants (Tenant 2, 10, 42).
3. Measures time-to-incorporate and lag_seconds for observer tenants.
4. Proves observer tenants' lag stays low (< 0.5s) and they are NOT starved by Tenant 1.
5. Ingests compensating deltas to restore baseline cleanly through outbox.
"""
import sys, os, time, datetime, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url
from tests.sync import wait_for_worker

def run_fairness_test(profile: str = "1m", flood_count: int = 1000) -> bool:
    print("=" * 70)
    print(f"RUNNING FAIRNESS & ANTI-STARVATION TEST ({profile.upper()})")
    print(f"Flooding Tenant 1 with {flood_count:,} outbox rows | Monitoring Tenants 2, 10, 42")
    print("=" * 70)

    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]
    flood_tid = 1
    observer_tids = [2, 10, 42]
    fairness_hour = "2026-10-29 12:00:00"

    flood_url = get_tenant_db_url(flood_tid, profile)
    f_conn = psycopg2.connect(flood_url)
    f_conn.autocommit = True
    f_cur = f_conn.cursor()

    # 1. Flood Tenant 1 outbox
    print(f"1. Injecting burst of {flood_count:,} outbox rows into Tenant {flood_tid}...")
    t0_flood = time.perf_counter()
    f_cur.execute("""
        INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
        SELECT 
            'order', 
            1, 
            %s::timestamp, 
            'delivered', 
            1, 
            10.00
        FROM generate_series(1, %s)
        RETURNING id
    """, (fairness_hour, flood_count))
    flood_row_ids = [r[0] for r in f_cur.fetchall()]
    max_flood_outbox_id = max(flood_row_ids)
    print(f"  Injected {flood_count:,} rows into Tenant {flood_tid} (max outbox id: {max_flood_outbox_id}) in {time.perf_counter() - t0_flood:.2f}s")

    # 2. Concurrently insert 1 order into each observer tenant with distinct commit timestamps
    print("2. Inserting orders into observer tenants (2, 10, 42) with distinct timestamps...")
    observer_oids = {}
    observer_conns = {}
    commit_times = {}
    test_ts = datetime.datetime(2026, 10, 25, 12, 0, 0)

    for otid in observer_tids:
        ourl = get_tenant_db_url(otid, profile)
        c = psycopg2.connect(ourl)
        c.autocommit = True
        cur = c.cursor()
        oid = 444_000 + otid
        observer_oids[otid] = oid
        cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
        cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))

        # Stagger slightly to prove individual timing
        time.sleep(0.02)
        t_commit = time.perf_counter()
        cur.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, %s, 1, 'delivered', 50.00, %s, %s)
        """, (oid, otid, test_ts, test_ts))
        cur.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, %s, 1, 1, 50.00)
        """, (oid, oid, otid))
        commit_times[otid] = t_commit
        observer_conns[otid] = (c, cur)

    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()

    try:
        # 3. Time each observer tenant separately to its own rollup visibility
        print("3. Timing observer tenants separately to their own rollup visibility...")
        observer_results = {}
        t_poll_start = time.perf_counter()

        while len(observer_results) < len(observer_tids) and (time.perf_counter() - t_poll_start) < 5.0:
            for otid in observer_tids:
                if otid in observer_results:
                    continue
                r_cur.execute("""
                    SELECT order_count FROM rollup_orders_hour 
                    WHERE tenant_id = %s AND hour = %s AND status = 'delivered'
                """, (otid, test_ts))
                res = r_cur.fetchone()
                if res and res[0] >= 1:
                    t_visible = time.perf_counter()
                    latency_sec = t_visible - commit_times[otid]
                    r_cur.execute("SELECT lag_seconds FROM worker_state WHERE tenant_id = %s", (otid,))
                    reported_lag = float(r_cur.fetchone()[0] or 0.0)
                    observer_results[otid] = (latency_sec, reported_lag)
            time.sleep(0.01)

        # 4. Report observer tenant timings
        print("\nOBSERVER TENANT TIMINGS (Individual Commit -> Rollup Visibility):")
        for otid in observer_tids:
            latency, reported_lag = observer_results.get(otid, (99.0, 99.0))
            print(f"  Tenant {otid:2d}: Ingestion latency = {latency*1000:.1f}ms | Worker lag_seconds = {reported_lag:.3f}s")
            assert latency < 1.0, f"Tenant {otid} was starved! Ingestion took {latency:.3f}s >= 1.0s"
            assert reported_lag < 0.5, f"Tenant {otid} lag too high: {reported_lag:.3f}s"

        # 5. Measure flooded Tenant 1 drain duration and track lag over time
        print("\n4. Monitoring flooded Tenant 1 draining {0:,} rows and sampling lag over time...".format(flood_count))
        t_drain_start = time.perf_counter()
        lag_samples = []

        while time.perf_counter() - t_drain_start < 15.0:
            elapsed = time.perf_counter() - t_drain_start
            r_cur.execute("SELECT cursor_id, lag_seconds, has_pending FROM worker_state WHERE tenant_id = %s", (flood_tid,))
            w_row = r_cur.fetchone()
            if w_row:
                cur_id, lag_val, pending = w_row[0], float(w_row[1] or 0.0), bool(w_row[2])
                lag_samples.append((round(elapsed, 2), round(lag_val, 3)))
                if not pending and cur_id >= max_flood_outbox_id:
                    break
            time.sleep(0.05)

        drain_duration = time.perf_counter() - t_drain_start
        peak_lag = max(s[1] for s in lag_samples) if lag_samples else 0.0
        print(f"  Flooded Tenant 1 drain completed in {drain_duration:.2f}s (Peak lag: {peak_lag:.2f}s)")
        print(f"  Tenant 1 lag progression over time: {lag_samples[:8]} ... {lag_samples[-3:]}")

    finally:
        # 5. Wait for Tenant 1 to finish clearing
        print("\n4. Waiting for flooded Tenant 1 to complete ingestion...")
        try:
            wait_for_worker(profile, tenant_id=flood_tid, timeout=15.0)

            # 6. Compensate Tenant 1 flood rows through outbox cleanly
            f_cur.execute("""
                INSERT INTO outbox (kind, tenant_id, bucket_ts, status, delta_count, delta_amount)
                SELECT 
                    'order', 
                    1, 
                    %s::timestamp, 
                    'delivered', 
                    -1, 
                    -10.00
                FROM generate_series(1, %s)
            """, (fairness_hour, flood_count))
            wait_for_worker(profile, tenant_id=flood_tid, timeout=15.0)
        except Exception as e:
            print(f"Cleanup warning for Tenant 1 flood: {e}")

        # Cleanup observer tenant orders
        for otid, (c, cur) in observer_conns.items():
            try:
                oid = observer_oids[otid]
                cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
                cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
                cur.close()
                c.close()
            except Exception:
                pass
        for otid in observer_tids:
            try:
                wait_for_worker(profile, tenant_id=otid, timeout=10.0)
            except Exception:
                pass

        f_cur.close()
        f_conn.close()
        r_cur.close()
        r_conn.close()

    print("\n>>> FAIRNESS & ANTI-STARVATION TEST PASSED 100% <<<")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    ok = run_fairness_test(profile=prof)
    sys.exit(0 if ok else 1)
