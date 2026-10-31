#!/usr/bin/env python3
"""scripts/rebuild.py:
Per-tenant rebuild function (B-2):
Opens a REPEATABLE READ snapshot on the tenant's DB,
aggregates orders and product sales directly, populates rollup tables,
and records the snapshot in that tenant's worker_state row.
Can run for a single tenant or in parallel across all tenants.
"""
import sys, os, time, io, csv, argparse
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_REPEATABLE_READ

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url, get_all_tenant_ids

def rebuild_tenant(tenant_id: int, profile: str = "1m", clear_first: bool = True) -> Dict:
    cfg = get_config(profile)
    excluded = tuple(cfg["business_rules"]["excluded_statuses"])
    t_start = time.time()

    # 1. Connect to tenant DB in REPEATABLE READ
    tenant_url = get_tenant_db_url(tenant_id, profile)
    t_conn = psycopg2.connect(tenant_url)
    t_conn.set_session(isolation_level=ISOLATION_LEVEL_REPEATABLE_READ, readonly=True)
    t_cur = t_conn.cursor()

    t_cur.execute("SELECT pg_current_snapshot()")
    snapshot_str = t_cur.fetchone()[0]

    # Find the maximum outbox row committed prior to this snapshot
    t_cur.execute("""
        SELECT COALESCE(MAX(id), 0), COALESCE(MAX(xid::text), '0')
        FROM outbox
        WHERE xid < pg_snapshot_xmin(%s::pg_snapshot)
    """, (snapshot_str,))
    max_safe_id, max_safe_xid = t_cur.fetchone()

    # 2. Query aggregated orders
    t_cur.execute("""
        SELECT 
            tenant_id,
            date_trunc('hour', created_at) AS hour,
            status,
            count(*) AS order_count,
            coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0) AS revenue
        FROM orders
        GROUP BY tenant_id, date_trunc('hour', created_at), status
    """, (excluded,))
    orders_rows = t_cur.fetchall()

    # 3. Query aggregated product sales
    t_cur.execute("""
        SELECT 
            oi.tenant_id,
            date_trunc('day', o.created_at) AS day,
            oi.product_id,
            p.category,
            sum(oi.qty) AS units,
            sum(oi.qty * oi.price) AS sales
        FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
        JOIN products p ON p.product_id = oi.product_id
        WHERE o.status NOT IN %s
        GROUP BY oi.tenant_id, date_trunc('day', o.created_at), oi.product_id, p.category
    """, (excluded,))
    items_rows = t_cur.fetchall()

    t_cur.close()
    t_conn.close()

    # 4. Connect to Rollup DB and apply atomically
    rollup_url = cfg["database"]["rollup_db_url"]
    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = False
    r_cur = r_conn.cursor()

    try:
        r_cur.execute("SELECT tenant_id FROM worker_state WHERE tenant_id = %s FOR UPDATE", (tenant_id,))
        if clear_first:
            r_cur.execute("DELETE FROM rollup_orders_hour WHERE tenant_id = %s", (tenant_id,))
            r_cur.execute("DELETE FROM rollup_product_day WHERE tenant_id = %s", (tenant_id,))

        # Stream orders_rows via COPY
        if orders_rows:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerows(orders_rows)
            buf.seek(0)
            r_cur.copy_expert("COPY rollup_orders_hour FROM STDIN WITH CSV", buf)
            buf.close()

        # Stream items_rows via COPY
        if items_rows:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerows(items_rows)
            buf.seek(0)
            r_cur.copy_expert("COPY rollup_product_day FROM STDIN WITH CSV", buf)
            buf.close()

        # Update worker_state for this tenant with safe cursor pre-advanced to snapshot xmin
        r_cur.execute("""
            INSERT INTO worker_state (
                tenant_id, cursor_xid, cursor_id, snapshot, last_batch_at, 
                lag_seconds, heartbeat, stalled, last_error, has_pending, oldest_pending_ts
            )
            VALUES (%s, %s::xid8, %s, %s::pg_snapshot, clock_timestamp(), 0.0, clock_timestamp(), false, NULL, false, NULL)
            ON CONFLICT (tenant_id)
            DO UPDATE SET
                cursor_xid = EXCLUDED.cursor_xid,
                cursor_id = EXCLUDED.cursor_id,
                snapshot = EXCLUDED.snapshot,
                last_batch_at = clock_timestamp(),
                lag_seconds = 0.0,
                heartbeat = clock_timestamp(),
                stalled = false,
                last_error = NULL,
                has_pending = false,
                oldest_pending_ts = NULL
        """, (tenant_id, max_safe_xid, max_safe_id, snapshot_str))

        r_conn.commit()
    except Exception as e:
        r_conn.rollback()
        raise e
    finally:
        r_cur.close()
        r_conn.close()

    elapsed = time.time() - t_start
    return {
        "tenant_id": tenant_id,
        "snapshot": snapshot_str,
        "hour_rows": len(orders_rows),
        "product_rows": len(items_rows),
        "elapsed_seconds": round(elapsed, 3)
    }

def rebuild_all(profile: str = "1m", tenant_count: int = 100, max_workers: int = 16):
    cfg = get_config(profile)
    t0 = time.time()
    print("=" * 70)
    print(f"STARTING FULL PARALLEL REBUILD (PROFILE: {profile.upper()})")
    print(f"Tenants: {tenant_count} | Parallel workers: {max_workers}")
    print("=" * 70)

    # Optional fast truncate
    rollup_url = cfg["database"]["rollup_db_url"]
    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()
    print("Truncating target rollup tables in rollup-db ...")
    r_cur.execute("TRUNCATE TABLE rollup_orders_hour, rollup_product_day")
    r_cur.close()
    r_conn.close()

    total_hour_rows = 0
    total_prod_rows = 0

    def worker_fn(tid: int):
        return rebuild_tenant(tid, profile=profile, clear_first=False)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        completed = 0
        for res in executor.map(worker_fn, range(1, tenant_count + 1)):
            completed += 1
            total_hour_rows += res["hour_rows"]
            total_prod_rows += res["product_rows"]
            if completed % 25 == 0 or completed == tenant_count:
                print(f"  Rebuilt {completed}/{tenant_count} tenants...")

    total_time = time.time() - t0

    # Post-rebuild VACUUM (ANALYZE) on rollup tables
    print("Running VACUUM (ANALYZE) on rollup tables...")
    try:
        cfg = get_config(profile)
        v_conn = psycopg2.connect(cfg["database"]["rollup_db_url"])
        v_conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        v_cur = v_conn.cursor()
        v_cur.execute("VACUUM (ANALYZE) rollup_orders_hour;")
        v_cur.execute("VACUUM (ANALYZE) rollup_product_day;")
        v_cur.close()
        v_conn.close()
        print("  VACUUM (ANALYZE) completed successfully.")
    except Exception as ve:
        print(f"  VACUUM warning: {ve}")

    print("=" * 70)
    print(f"FULL REBUILD COMPLETE in {total_time:.2f}s")
    print(f"  Total rollup_orders_hour: {total_hour_rows:,}")
    print(f"  Total rollup_product_day: {total_prod_rows:,}")
    print("=" * 70)
    return total_time

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebuild rollups per tenant or all tenants.")
    parser.add_argument("--profile", default="1m", choices=["1m", "10m"], help="Profile")
    parser.add_argument("--tenant-id", type=int, default=None, help="Specific tenant ID to rebuild")
    parser.add_argument("--tenants", type=int, default=100, help="Total tenant count if rebuilding all")
    parser.add_argument("--workers", type=int, default=16, help="Parallel worker threads")
    args = parser.parse_args()

    if args.tenant_id is not None:
        print(f"Rebuilding single tenant {args.tenant_id}...")
        res = rebuild_tenant(args.tenant_id, profile=args.profile)
        print("Result:", res)
    else:
        rebuild_all(profile=args.profile, tenant_count=args.tenants, max_workers=args.workers)
