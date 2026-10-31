#!/usr/bin/env python3
"""worker/worker.py:
Multi-source asynchronous rollup worker:
- Persistent connection per tenant DB (raises max_connections in main-db).
- Round-robin visit per tenant DB with per-tenant safe-xid batches:
  (xid < pg_snapshot_xmin(pg_current_snapshot()) ORDER BY xid, id).
- Rows per tenant visit capped so high-volume flooding tenant cannot starve others.
- In-memory folding by rollup key.
- One upsert per key into rollup-db and cursor update in the same transaction.
- Per-tenant fail-closed semantics: constraint violation halts ONLY that tenant; other tenants continue.
- Sharding via WORKER_ID and WORKER_COUNT (tenant_id % WORKER_COUNT == WORKER_ID).
"""
import sys, os, time, json, datetime, yaml, argparse
from typing import Dict, Tuple, List, Optional
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url, get_all_tenant_ids

class MultiTenantWorker:
    def __init__(
        self,
        profile: str = "1m",
        worker_id: int = 0,
        worker_count: int = 1,
        per_tenant_cap: int = 500,
        poll_interval: float = 0.05,
        target_tenants: Optional[List[int]] = None
    ):
        self.profile = profile
        self.worker_id = worker_id
        self.worker_count = worker_count
        self.per_tenant_cap = per_tenant_cap
        self.poll_interval = poll_interval
        self.cfg = get_config(profile)
        self.rollup_url = self.cfg["database"]["rollup_db_url"]
        self.all_statuses = set(self.cfg["business_rules"]["all_statuses"])

        # Determine owned tenants
        all_tids = target_tenants or get_all_tenant_ids(self.cfg.get("tenant_db", {}).get("count", 100))
        self.owned_tenants = [t for t in all_tids if (t % self.worker_count) == self.worker_id]

        print(f"=== Multi-Tenant Rollup Worker ===")
        print(f"Profile: {self.profile} | Worker ID: {self.worker_id}/{self.worker_count}")
        print(f"Owned Tenants: {len(self.owned_tenants)} tenants")
        print(f"Rollup DB: {self.rollup_url}")

        # Connect to rollup DB
        self.r_conn = psycopg2.connect(self.rollup_url)
        self.r_conn.autocommit = False
        self.r_cur = self.r_conn.cursor()

        # Connect to each owned tenant DB persistently
        self.tenant_conns: Dict[int, psycopg2.extensions.connection] = {}
        self.tenant_curs: Dict[int, psycopg2.extensions.cursor] = {}
        self.stalled_tenants = set()
        self.batch_counts: Dict[int, int] = {t: 0 for t in self.owned_tenants}

        self._connect_all_tenants()

    def _connect_all_tenants(self):
        print(f"Establishing persistent connections to {len(self.owned_tenants)} tenant DBs...")
        t0 = time.time()
        for tid in self.owned_tenants:
            try:
                db_url = get_tenant_db_url(tid, self.profile)
                c = psycopg2.connect(db_url)
                c.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                self.tenant_conns[tid] = c
                self.tenant_curs[tid] = c.cursor()
            except Exception as e:
                print(f"[WARN] Failed to connect to tenant {tid}: {e}")
        print(f"Connected to {len(self.tenant_conns)} tenant DBs in {time.time() - t0:.2f}s")

    def _get_tenant_cursor_and_state(self, tid: int):
        self.r_cur.execute("""
            SELECT cursor_xid::text, cursor_id, snapshot::text, stalled
            FROM worker_state
            WHERE tenant_id = %s
        """, (tid,))
        row = self.r_cur.fetchone()
        if not row:
            return '0', 0, None, False
        return row[0], row[1], row[2], bool(row[3])

    def _record_tenant_stall(self, tid: int, error_msg: str):
        print(f"[FAIL CLOSED] Tenant {tid} stalled due to integrity violation: {error_msg}")
        self.stalled_tenants.add(tid)
        try:
            self.r_conn.rollback()
            stall_conn = psycopg2.connect(self.rollup_url)
            stall_conn.autocommit = True
            scur = stall_conn.cursor()
            scur.execute("""
                UPDATE worker_state SET
                    stalled = true,
                    last_error = %s,
                    heartbeat = clock_timestamp()
                WHERE tenant_id = %s
            """, (error_msg, tid))
            scur.close()
            stall_conn.close()
        except Exception as se:
            print(f"[WORKER ERROR] Failed to record stall for tenant {tid}: {se}")

    def run_one_cycle(self) -> Dict[str, any]:
        t_cycle_start = time.perf_counter()
        tenants_with_activity = 0
        total_rows_ingested = 0

        for tid in self.owned_tenants:
            # Skip if locally known as stalled
            if tid in self.stalled_tenants:
                continue

            cursor_xid, cursor_id, snapshot_str, stalled = self._get_tenant_cursor_and_state(tid)
            if stalled:
                self.stalled_tenants.add(tid)
                continue

            t_cur = self.tenant_curs.get(tid)
            if not t_cur or t_cur.closed:
                try:
                    c = psycopg2.connect(get_tenant_db_url(tid, self.profile))
                    c.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                    self.tenant_conns[tid] = c
                    t_cur = self.tenant_curs[tid] = c.cursor()
                except Exception:
                    continue

            # Query safe outbox rows for this tenant
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
            try:
                t_cur.execute(query, (
                    snapshot_str, snapshot_str, snapshot_str,
                    cursor_xid, cursor_xid, cursor_id,
                    self.per_tenant_cap
                ))
                rows = t_cur.fetchall()
            except Exception as read_err:
                # Connection or outbox query error on tenant DB
                continue

            if not rows:
                continue

            tenants_with_activity += 1
            total_rows_ingested += len(rows)

            # In-memory folding for this tenant
            orders_folded: Dict[Tuple[int, datetime.datetime, str], Dict] = {}
            items_folded: Dict[Tuple[int, datetime.datetime, int, str], Dict] = {}
            quarantine_records = []

            last_xid = cursor_xid
            last_id = cursor_id
            now_ts = datetime.datetime.now(datetime.timezone.utc)

            for r in rows:
                (row_id, r_xid, kind, r_tid, bucket_ts, status, 
                 prod_id, cat, d_cnt, d_amt, created_at, in_snapshot) = r

                last_xid = r_xid
                last_id = row_id

                if in_snapshot:
                    continue

                # Validation & Quarantine
                bad_reason = None
                if r_tid is None or r_tid <= 0:
                    bad_reason = f"Invalid tenant_id: {r_tid}"
                elif bucket_ts is None:
                    bad_reason = "Missing bucket_ts"
                elif kind == "order":
                    if status not in self.all_statuses:
                        bad_reason = f"Unknown order status: {status}"
                elif kind == "item":
                    if prod_id is None or prod_id <= 0:
                        bad_reason = f"Invalid product_id: {prod_id}"
                else:
                    bad_reason = f"Unknown outbox kind: {kind}"

                if bad_reason:
                    payload = {
                        "id": row_id, "xid": r_xid, "kind": kind, "tenant_id": r_tid,
                        "bucket_ts": str(bucket_ts), "status": status, "product_id": prod_id,
                        "category": cat, "delta_count": d_cnt, "delta_amount": float(d_amt)
                    }
                    quarantine_records.append((r_tid or tid, row_id, json.dumps(payload), bad_reason))
                    continue

                # Accumulate deltas
                if kind == "order":
                    key = (tid, bucket_ts, status)
                    if key not in orders_folded:
                        orders_folded[key] = {"count": 0, "revenue": 0.0}
                    orders_folded[key]["count"] += d_cnt
                    orders_folded[key]["revenue"] += float(d_amt)
                elif kind == "item":
                    key = (tid, bucket_ts, prod_id, cat)
                    if key not in items_folded:
                        items_folded[key] = {"units": 0, "sales": 0.0}
                    items_folded[key]["units"] += d_cnt
                    items_folded[key]["sales"] += float(d_amt)

            # Check for further pending rows on this tenant
            try:
                t_cur.execute("""
                    SELECT created_at FROM outbox
                    WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
                      AND (xid > %s::xid8 OR (xid = %s::xid8 AND id > %s))
                    ORDER BY xid ASC, id ASC
                    LIMIT 1
                """, (last_xid, last_xid, last_id))
                pending_row = t_cur.fetchone()
                if pending_row:
                    has_pending = True
                    oldest_ts = pending_row[0]
                    dt = (now_ts - oldest_ts.replace(tzinfo=datetime.timezone.utc)).total_seconds() if oldest_ts.tzinfo else (datetime.datetime.now() - oldest_ts).total_seconds()
                    lag_sec = max(0.0, round(dt, 2))
                else:
                    has_pending = False
                    oldest_ts = None
                    lag_sec = 0.0
            except Exception:
                has_pending = False
                oldest_ts = None
                lag_sec = 0.0

            # Atomic apply to Rollup DB in single transaction for this tenant
            try:
                # Lock and verify state hasn't changed concurrently (e.g. rebuild intervened)
                self.r_cur.execute("""
                    SELECT cursor_xid::text, cursor_id, snapshot::text, stalled
                    FROM worker_state
                    WHERE tenant_id = %s
                    FOR UPDATE
                """, (tid,))
                cur_state = self.r_cur.fetchone()
                if cur_state:
                    cur_xid_now, cur_id_now, snap_now, is_stalled = cur_state
                    if is_stalled or snap_now != snapshot_str or (cur_xid_now, cur_id_now) != (cursor_xid, cursor_id):
                        self.r_conn.rollback()
                        continue

                # Apply folded orders
                for (t_id, hr, st), deltas in orders_folded.items():
                    self.r_cur.execute("""
                        UPDATE rollup_orders_hour SET
                            order_count = order_count + %s,
                            revenue = revenue + %s
                        WHERE tenant_id = %s AND hour = %s AND status = %s
                    """, (deltas["count"], deltas["revenue"], t_id, hr, st))
                    if self.r_cur.rowcount == 0:
                        self.r_cur.execute("""
                            INSERT INTO rollup_orders_hour (tenant_id, hour, status, order_count, revenue)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (tenant_id, hour, status)
                            DO UPDATE SET
                                order_count = rollup_orders_hour.order_count + %s,
                                revenue = rollup_orders_hour.revenue + %s
                        """, (t_id, hr, st, deltas["count"], deltas["revenue"], deltas["count"], deltas["revenue"]))

                # Apply folded items
                for (t_id, day, pid, cat), deltas in items_folded.items():
                    self.r_cur.execute("""
                        UPDATE rollup_product_day SET
                            units = units + %s,
                            sales = sales + %s
                        WHERE tenant_id = %s AND day = %s AND product_id = %s
                    """, (deltas["units"], deltas["sales"], t_id, day, pid))
                    if self.r_cur.rowcount == 0:
                        self.r_cur.execute("""
                            INSERT INTO rollup_product_day (tenant_id, day, product_id, category, units, sales)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (tenant_id, day, product_id)
                            DO UPDATE SET
                                units = rollup_product_day.units + %s,
                                sales = rollup_product_day.sales + %s
                        """, (t_id, day, pid, cat, deltas["units"], deltas["sales"], deltas["units"], deltas["sales"]))

                # Apply quarantine records
                for q_tid, q_oid, q_payload, q_reason in quarantine_records:
                    self.r_cur.execute("""
                        INSERT INTO quarantine (tenant_id, outbox_id, payload, reason)
                        VALUES (%s, %s, %s, %s)
                    """, (q_tid, q_oid, q_payload, q_reason))

                # Update this tenant's cursor in worker_state
                self.r_cur.execute("""
                    UPDATE worker_state SET
                        cursor_xid = %s::xid8,
                        cursor_id = %s,
                        last_batch_at = clock_timestamp(),
                        lag_seconds = %s,
                        heartbeat = clock_timestamp(),
                        stalled = false,
                        last_error = NULL,
                        has_pending = %s,
                        oldest_pending_ts = %s
                    WHERE tenant_id = %s
                """, (last_xid, last_id, lag_sec, has_pending, oldest_ts, tid))

                self.r_conn.commit()

                # Periodically prune outbox on this tenant DB
                self.batch_counts[tid] += 1
                if self.batch_counts[tid] % 20 == 0:
                    try:
                        t_cur.execute("""
                            DELETE FROM outbox 
                            WHERE xid < %s::xid8 OR (xid = %s::xid8 AND id <= %s)
                        """, (last_xid, last_id))
                    except Exception:
                        pass

            except Exception as apply_err:
                self.r_conn.rollback()
                self._record_tenant_stall(tid, str(apply_err))

        cycle_duration = time.perf_counter() - t_cycle_start
        return {
            "duration": cycle_duration,
            "active_tenants": tenants_with_activity,
            "rows_ingested": total_rows_ingested
        }

    def update_idle_heartbeats(self):
        try:
            self.r_cur.execute("""
                UPDATE worker_state SET
                    heartbeat = clock_timestamp(),
                    lag_seconds = CASE WHEN has_pending THEN lag_seconds ELSE 0.0 END
                WHERE tenant_id = ANY(%s) AND NOT stalled
            """, (self.owned_tenants,))
            self.r_conn.commit()
        except Exception:
            self.r_conn.rollback()

    def run_forever(self):
        print(f"[WORKER] Starting round-robin ingestion loop for {len(self.owned_tenants)} tenants...")
        last_hb = time.time()
        while True:
            try:
                res = self.run_one_cycle()
                now = time.time()
                if now - last_hb > 1.0:
                    self.update_idle_heartbeats()
                    last_hb = now

                if res["active_tenants"] == 0:
                    time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                print("\n[WORKER] Stopped by operator.")
                break
            except Exception as loop_err:
                print(f"[WORKER CRITICAL] Unexpected loop error: {loop_err}")
                time.sleep(1.0)

        self.close()

    def close(self):
        try:
            self.r_cur.close()
            self.r_conn.close()
        except Exception:
            pass
        for cur in self.tenant_curs.values():
            try: cur.close()
            except Exception: pass
        for conn in self.tenant_conns.values():
            try: conn.close()
            except Exception: pass

def benchmark_cycles(profile: str = "1m", tenant_counts: List[int] = [10, 50, 100], rounds: int = 50):
    print("=" * 70)
    print(f"BENCHMARKING WORKER CYCLE TIME vs NUMBER OF TENANTS (PROFILE: {profile.upper()})")
    print(f"Rounds: {rounds} per measurement")
    print("=" * 70)

    for n in tenant_counts:
        tids = list(range(1, n + 1))
        worker = MultiTenantWorker(profile=profile, target_tenants=tids, poll_interval=0.0)
        times = []
        for _ in range(rounds):
            res = worker.run_one_cycle()
            times.append(res["duration"] * 1000.0)  # ms
        worker.close()

        import numpy as np
        p50 = np.percentile(times, 50)
        p95 = np.percentile(times, 95)
        avg = np.mean(times)
        print(f"Tenants: {n:3d} | Avg Cycle: {avg:6.2f} ms | p50: {p50:6.2f} ms | p95: {p95:6.2f} ms")
    print("=" * 70)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-source multi-tenant rollup worker.")
    parser.add_argument("--profile", default=os.getenv("PROFILE", "1m"), choices=["1m", "10m"], help="Dataset profile")
    parser.add_argument("--worker-id", type=int, default=int(os.getenv("WORKER_ID", "0")), help="Worker ID for sharding")
    parser.add_argument("--worker-count", type=int, default=int(os.getenv("WORKER_COUNT", "1")), help="Total workers for sharding")
    parser.add_argument("--batch-cap", type=int, default=500, help="Max rows per tenant visit")
    parser.add_argument("--poll-interval", type=float, default=0.05, help="Idle sleep interval (seconds)")
    parser.add_argument("--benchmark-cycles", action="store_true", help="Run cycle time benchmark for 10, 50, 100 tenants")
    args = parser.parse_args()

    if args.benchmark_cycles:
        benchmark_cycles(profile=args.profile)
    else:
        w = MultiTenantWorker(
            profile=args.profile,
            worker_id=args.worker_id,
            worker_count=args.worker_count,
            per_tenant_cap=args.batch_cap,
            poll_interval=args.poll_interval
        )
        w.run_forever()
