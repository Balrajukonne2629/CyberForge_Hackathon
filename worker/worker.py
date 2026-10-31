#!/usr/bin/env python3
"""Asynchronous rollup worker:
- Safe read using xid < pg_snapshot_xmin(pg_current_snapshot()) ordered by (xid, id)
- In-memory folding by rollup key
- Atomic upsert and cursor save in the same transaction
- Fail closed with CHECK (count >= 0) and worker_stalled flag
- Quarantine for malformed/corrupted rows
- Lag monitoring and heartbeat in worker_state
"""
import sys, os, time, json, datetime, yaml, psycopg2
from typing import Dict, Tuple

def run_worker(main_url: str, rollup_url: str, batch_size: int = 5000, poll_interval: float = 0.2):
    print("=== Starting Rollup Worker ===")
    print(f"Main DB: {main_url}")
    print(f"Rollup DB: {rollup_url}")

    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    all_statuses = set(cfg["business_rules"]["all_statuses"])

    main_conn = psycopg2.connect(main_url)
    main_conn.autocommit = True
    main_cur = main_conn.cursor()

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_conn.autocommit = False
    rollup_cur = rollup_conn.cursor()

    batches_processed = 0

    while True:
        try:
            # 1. Fetch current cursor and snapshot from worker_state
            rollup_cur.execute("""
                SELECT cursor_xid::text, cursor_id, snapshot::text, worker_stalled 
                FROM worker_state WHERE id = 1
            """)
            state_row = rollup_cur.fetchone()
            if not state_row:
                time.sleep(1)
                continue

            cursor_xid, cursor_id, snapshot_str, worker_stalled = state_row
            if worker_stalled:
                print("[WORKER] Worker is stalled due to previous error. Halting loop.")
                break

            # 2. Query safe outbox rows from main-db
            # Must be strictly below pg_snapshot_xmin(pg_current_snapshot())
            query = """
                SELECT 
                    id, xid::text, kind, tenant_id, bucket_ts, status, product_id, category, 
                    delta_count, delta_amount, created_at,
                    CASE 
                        WHEN %s IS NOT NULL AND %s != '' THEN pg_visible_in_snapshot(xid, %s::pg_snapshot)
                        ELSE false
                    END AS in_rebuild_snapshot
                FROM outbox
                WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
                  AND (xid > %s::xid8 OR (xid = %s::xid8 AND id > %s))
                ORDER BY xid ASC, id ASC
                LIMIT %s
            """
            main_cur.execute(query, (
                snapshot_str, snapshot_str, snapshot_str,
                cursor_xid, cursor_xid, cursor_id,
                batch_size
            ))
            rows = main_cur.fetchall()

            now_ts = datetime.datetime.now(datetime.timezone.utc)

            if not rows:
                # Update heartbeat and lag_seconds = 0 when idle
                rollup_cur.execute("""
                    UPDATE worker_state SET 
                        heartbeat = clock_timestamp(),
                        lag_seconds = 0.0
                    WHERE id = 1
                """)
                rollup_conn.commit()
                time.sleep(poll_interval)
                continue

            # 3. Process batch and fold in memory
            orders_folded: Dict[Tuple[int, datetime.datetime, str], Dict] = {}
            items_folded: Dict[Tuple[int, datetime.datetime, int, str], Dict] = {}
            quarantine_records = []

            last_xid = cursor_xid
            last_id = cursor_id
            latest_row_created = None

            for r in rows:
                (row_id, r_xid, kind, tenant_id, bucket_ts, status, 
                 prod_id, cat, d_cnt, d_amt, created_at, in_snapshot) = r

                last_xid = r_xid
                last_id = row_id
                if latest_row_created is None or created_at > latest_row_created:
                    latest_row_created = created_at

                # Skip if already visible in the rebuild snapshot
                if in_snapshot:
                    continue

                # Validation & Quarantine
                bad_reason = None
                if tenant_id is None or tenant_id <= 0:
                    bad_reason = f"Invalid tenant_id: {tenant_id}"
                elif bucket_ts is None:
                    bad_reason = "Missing bucket_ts"
                elif kind == "order":
                    if status not in all_statuses:
                        bad_reason = f"Unknown order status: {status}"
                elif kind == "item":
                    if prod_id is None or prod_id <= 0:
                        bad_reason = f"Invalid product_id: {prod_id}"
                else:
                    bad_reason = f"Unknown outbox kind: {kind}"

                if bad_reason:
                    payload = {
                        "id": row_id, "xid": r_xid, "kind": kind, "tenant_id": tenant_id,
                        "bucket_ts": str(bucket_ts), "status": status, "product_id": prod_id,
                        "category": cat, "delta_count": d_cnt, "delta_amount": float(d_amt)
                    }
                    quarantine_records.append((row_id, json.dumps(payload), bad_reason))
                    continue

                # Fold into memory accumulators
                if kind == "order":
                    key = (tenant_id, bucket_ts, status)
                    if key not in orders_folded:
                        orders_folded[key] = {"count": 0, "revenue": 0.0}
                    orders_folded[key]["count"] += d_cnt
                    orders_folded[key]["revenue"] += float(d_amt)
                elif kind == "item":
                    key = (tenant_id, bucket_ts, prod_id, cat)
                    if key not in items_folded:
                        items_folded[key] = {"units": 0, "sales": 0.0}
                    items_folded[key]["units"] += d_cnt
                    items_folded[key]["sales"] += float(d_amt)

            # Calculate lag
            lag_sec = 0.0
            if latest_row_created:
                # Naive or UTC timestamp difference
                if latest_row_created.tzinfo is None:
                    dt = datetime.datetime.now() - latest_row_created
                else:
                    dt = now_ts - latest_row_created
                lag_sec = max(0.0, round(dt.total_seconds(), 2))

            # 4. Atomic apply in Rollup DB transaction
            try:
                # Apply folded orders
                for (tid, hr, st), deltas in orders_folded.items():
                    rollup_cur.execute("""
                        UPDATE rollup_orders_hour SET
                            order_count = order_count + %s,
                            revenue = revenue + %s
                        WHERE tenant_id = %s AND hour = %s AND status = %s
                    """, (deltas["count"], deltas["revenue"], tid, hr, st))
                    if rollup_cur.rowcount == 0:
                        rollup_cur.execute("""
                            INSERT INTO rollup_orders_hour (tenant_id, hour, status, order_count, revenue)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (tid, hr, st, deltas["count"], deltas["revenue"]))

                # Apply folded items
                for (tid, day, pid, cat), deltas in items_folded.items():
                    rollup_cur.execute("""
                        UPDATE rollup_product_day SET
                            units = units + %s,
                            sales = sales + %s
                        WHERE tenant_id = %s AND day = %s AND product_id = %s
                    """, (deltas["units"], deltas["sales"], tid, day, pid))
                    if rollup_cur.rowcount == 0:
                        rollup_cur.execute("""
                            INSERT INTO rollup_product_day (tenant_id, day, product_id, category, units, sales)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (tid, day, pid, cat, deltas["units"], deltas["sales"]))

                # Insert quarantine rows
                for q_oid, q_payload, q_reason in quarantine_records:
                    rollup_cur.execute("""
                        INSERT INTO quarantine (outbox_id, payload, reason)
                        VALUES (%s, %s, %s)
                    """, (q_oid, q_payload, q_reason))

                # Update cursor and state
                rollup_cur.execute("""
                    UPDATE worker_state SET
                        cursor_xid = %s::xid8,
                        cursor_id = %s,
                        last_batch_at = clock_timestamp(),
                        lag_seconds = %s,
                        heartbeat = clock_timestamp(),
                        worker_stalled = false,
                        stalled_reason = NULL
                    WHERE id = 1
                """, (last_xid, last_id, lag_sec))

                rollup_conn.commit()

            except Exception as apply_err:
                rollup_conn.rollback()
                err_msg = str(apply_err)
                print(f"[WORKER CRITICAL] Failed to apply batch: {err_msg}")
                # Set worker_stalled flag
                try:
                    rollup_cur.execute("""
                        UPDATE worker_state SET
                            heartbeat = clock_timestamp(),
                            worker_stalled = true,
                            stalled_reason = %s
                        WHERE id = 1
                    """, (err_msg,))
                    rollup_conn.commit()
                except Exception as stall_err:
                    print(f"[WORKER] Failed to record stall: {stall_err}")
                raise apply_err

            batches_processed += 1

            # 5. Periodically prune processed outbox rows from main-db
            if batches_processed % 20 == 0:
                main_cur.execute("""
                    DELETE FROM outbox 
                    WHERE xid < %s::xid8 OR (xid = %s::xid8 AND id <= %s)
                """, (last_xid, last_id))

        except psycopg2.Error as pg_err:
            if "check constraint" in str(pg_err).lower() or "checkviolation" in str(pg_err).lower():
                print(f"[WORKER HALTED] Negative count constraint violated! Fail-closed: {pg_err}")
                break
            print(f"[WORKER ERROR] Database error: {pg_err}")
            time.sleep(1)
        except Exception as e:
            print(f"[WORKER ERROR] Unexpected error: {e}")
            time.sleep(1)

    rollup_cur.close()
    rollup_conn.close()
    main_cur.close()
    main_conn.close()

if __name__ == "__main__":
    m_url = sys.argv[1] if len(sys.argv) > 1 else os.getenv("MAIN_DB_URL", "postgresql://postgres:postgres@localhost:5432/shop")
    r_url = sys.argv[2] if len(sys.argv) > 2 else os.getenv("ROLLUP_DB_URL", "postgresql://postgres:postgres@localhost:5433/rollup_1m")
    run_worker(m_url, r_url)
