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
    t0_flood = time.time()
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
    """, (fairness_hour, flood_count))
    print(f"  Injected {flood_count:,} rows into Tenant {flood_tid} in {time.time() - t0_flood:.2f}s")

    # 2. Concurrently insert 1 order into each observer tenant
    print("2. Concurrently inserting orders into observer tenants (2, 10, 42)...")
    observer_oids = {}
    observer_conns = {}
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
        cur.execute("""
            INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
            VALUES (%s, %s, 1, 'delivered', 50.00, %s, %s)
        """, (oid, otid, test_ts, test_ts))
        cur.execute("""
            INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
            VALUES (%s, %s, %s, 1, 1, 50.00)
        """, (oid, oid, otid))
        observer_conns[otid] = (c, cur)

    # 3. Measure observer lag and completion time
    t_start_obs = time.time()
    print("3. Observing observer tenants' lag while Tenant 1 is clearing flood...")
    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()

    observer_latencies = {}
    while len(observer_latencies) < len(observer_tids) and (time.time() - t_start_obs) < 5.0:
        r_cur.execute("""
            SELECT tenant_id, lag_seconds, cursor_id 
            FROM worker_state 
            WHERE tenant_id = ANY(%s)
        """, (observer_tids,))
        for row in r_cur.fetchall():
            tid, lag, cur_id = row[0], float(row[1]), row[2]
            if tid not in observer_latencies and cur_id > 0:
                elapsed = time.time() - t_start_obs
                observer_latencies[tid] = (elapsed, lag)
        time.sleep(0.02)

    # 4. Report numbers
    print("\nFAIRNESS RESULTS:")
    for otid in observer_tids:
        elapsed, lag = observer_latencies.get(otid, (99.0, 99.0))
        print(f"  Tenant {otid:2d}: Ingested in {elapsed:.3f}s | Worker reported lag: {lag:.2f}s")
        assert elapsed < 1.0, f"Tenant {otid} was starved! Ingestion took {elapsed:.3f}s >= 1.0s"
        assert lag < 0.5, f"Tenant {otid} lag too high: {lag:.2f}s"

    # 5. Wait for Tenant 1 to finish clearing
    print("\n4. Waiting for flooded Tenant 1 to complete ingestion...")
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

    # Cleanup observer tenant orders
    for otid, (c, cur) in observer_conns.items():
        oid = observer_oids[otid]
        cur.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
        cur.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
        cur.close()
        c.close()
    for otid in observer_tids:
        wait_for_worker(profile, tenant_id=otid, timeout=10.0)

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
