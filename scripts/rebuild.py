#!/usr/bin/env python3
"""Rebuild procedure (B-2):
Opens a REPEATABLE READ transaction on main-db, captures pg_current_snapshot(),
aggregates orders and product sales directly, populates rollup tables, and
records the snapshot in worker_state.
"""
import sys, time, yaml, psycopg2

def rebuild(main_url: str, rollup_url: str):
    print(f"=== Starting Rebuild Procedure ===")
    print(f"Source Main DB: {main_url}")
    print(f"Target Rollup DB: {rollup_url}")
    t0 = time.time()

    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    excluded = tuple(cfg["business_rules"]["excluded_statuses"])

    # 1. Connect to main-db with REPEATABLE READ
    main_conn = psycopg2.connect(main_url)
    main_conn.set_session(isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ, readonly=True)
    main_cur = main_conn.cursor()

    main_cur.execute("SELECT pg_current_snapshot()")
    snapshot_str = main_cur.fetchone()[0]
    print(f"Captured REPEATABLE READ snapshot on main-db: {snapshot_str}")

    # 2. Connect to rollup-db
    rollup_conn = psycopg2.connect(rollup_url)
    rollup_conn.autocommit = False
    rollup_cur = rollup_conn.cursor()

    # Truncate existing rollups in target DB
    print("Truncating target rollup tables...")
    rollup_cur.execute("TRUNCATE TABLE rollup_orders_hour, rollup_product_day")

    # 3. Aggregate rollup_orders_hour
    print("Aggregating rollup_orders_hour from main-db...")
    t_hour = time.time()
    main_cur.execute("""
        SELECT 
            tenant_id,
            date_trunc('hour', created_at) AS hour,
            status,
            count(*) AS order_count,
            coalesce(sum(CASE WHEN status NOT IN %s THEN total ELSE 0 END), 0) AS revenue
        FROM orders
        GROUP BY tenant_id, date_trunc('hour', created_at), status
    """, (excluded,))

    # Stream insert via COPY or execute_batch
    import io, csv
    buf = io.StringIO()
    writer = csv.writer(buf)
    count_hour_rows = 0
    for row in main_cur:
        writer.writerow(row)
        count_hour_rows += 1
        if count_hour_rows % 50000 == 0:
            buf.seek(0)
            rollup_cur.copy_expert("COPY rollup_orders_hour FROM STDIN WITH CSV", buf)
            buf.seek(0)
            buf.truncate(0)

    buf.seek(0)
    rollup_cur.copy_expert("COPY rollup_orders_hour FROM STDIN WITH CSV", buf)
    buf.close()
    print(f"  Populated {count_hour_rows:,} rollup_orders_hour rows in {time.time() - t_hour:.2f}s")

    # 4. Aggregate rollup_product_day (only counted orders)
    print("Aggregating rollup_product_day from main-db...")
    t_prod = time.time()
    main_cur.execute("""
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

    buf = io.StringIO()
    writer = csv.writer(buf)
    count_prod_rows = 0
    for row in main_cur:
        writer.writerow(row)
        count_prod_rows += 1
        if count_prod_rows % 50000 == 0:
            buf.seek(0)
            rollup_cur.copy_expert("COPY rollup_product_day FROM STDIN WITH CSV", buf)
            buf.seek(0)
            buf.truncate(0)

    buf.seek(0)
    rollup_cur.copy_expert("COPY rollup_product_day FROM STDIN WITH CSV", buf)
    buf.close()
    print(f"  Populated {count_prod_rows:,} rollup_product_day rows in {time.time() - t_prod:.2f}s")

    # 5. Store snapshot in worker_state
    print("Updating worker_state with snapshot...")
    rollup_cur.execute("""
        UPDATE worker_state SET
            snapshot = %s,
            cursor_xid = '0'::xid8,
            cursor_id = 0,
            last_batch_at = clock_timestamp(),
            lag_seconds = 0.0,
            heartbeat = clock_timestamp(),
            worker_stalled = false,
            stalled_reason = NULL
        WHERE id = 1
    """, (snapshot_str,))

    rollup_conn.commit()
    main_cur.close()
    main_conn.close()
    rollup_cur.close()
    rollup_conn.close()

    total_time = time.time() - t0
    print(f"=== Rebuild Complete in {total_time:.2f}s ===\n")

if __name__ == "__main__":
    main_url = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres:postgres@localhost:5432/shop"
    rollup_url = sys.argv[2] if len(sys.argv) > 2 else "postgresql://postgres:postgres@localhost:5433/rollup_1m"
    rebuild(main_url, rollup_url)
