#!/usr/bin/env python3
"""worker/worker.py:
Multi-source asynchronous rollup worker:
- Supports WORKER_MODE=event|poll (default: event).
- Event mode: Persistent connection per tenant DB with LISTEN outbox, selectors waiting on sockets.
- Debounce (10-20ms) coalesces bursts into a ready set of dirty tenants.
- Idle tenants cost 0 queries/cycle.
- Safety sweep at startup, on reconnect, and every 5.0s (configurable).
- Deficit Round-Robin (DRR) time-slice fairness: unthrottled when alone, fair when congested.
- Safe-xid multi-tenant batch ingestion with folding and atomic cursor advance.
- Per-tenant fail-closed constraint isolation.
- Push: emits pg_notify('rollup_updated', tenant_id) on rollup-db upon successful batch.
- Sharding via WORKER_ID and WORKER_COUNT.
"""
import sys, os, time, json, datetime, yaml, argparse, selectors
from typing import Dict, Tuple, List, Optional, Set
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
        mode: str = "event",
        per_tenant_cap: int = 500,
        poll_interval: float = 0.5,
        safety_sweep_interval: float = 5.0,
        debounce_ms: float = 15.0,
        target_tenants: Optional[List[int]] = None
    ):
        self.profile = profile
        self.worker_id = worker_id
        self.worker_count = worker_count
        self.mode = mode.lower()
        self.per_tenant_cap = per_tenant_cap
        self.poll_interval = poll_interval
        self.safety_sweep_interval = safety_sweep_interval
        self.debounce_ms = debounce_ms
        self.cfg = get_config(profile)
        self.rollup_url = self.cfg["database"]["rollup_db_url"]
        self.all_statuses = set(self.cfg["business_rules"]["all_statuses"])

        # Determine owned tenants
        all_tids = target_tenants or get_all_tenant_ids(self.cfg.get("tenant_db", {}).get("count", 100))
        self.owned_tenants = [t for t in all_tids if (t % self.worker_count) == self.worker_id]

        print(f"=== Multi-Tenant Rollup Worker ===")
        print(f"Profile: {self.profile} | Mode: {self.mode.upper()} | Worker ID: {self.worker_id}/{self.worker_count}")
        print(f"Owned Tenants: {len(self.owned_tenants)} tenants")
        print(f"Rollup DB: {self.rollup_url}")

        # Connect to rollup DB
        self.r_conn = psycopg2.connect(self.rollup_url)
        self.r_conn.autocommit = False
        self.r_cur = self.r_conn.cursor()

        # Connect to each owned tenant DB persistently
        self.tenant_conns: Dict[int, psycopg2.extensions.connection] = {}
        self.tenant_curs: Dict[int, psycopg2.extensions.cursor] = {}
        self.stalled_tenants: Set[int] = set()
        self.batch_counts: Dict[int, int] = {t: 0 for t in self.owned_tenants}
        self.deficit: Dict[int, int] = {t: 0 for t in self.owned_tenants}
        self.dirty_tenants: Set[int] = set(self.owned_tenants)  # Initial safety sweep on startup

        self.selector = selectors.DefaultSelector()
        self.start_time = time.time()
        self.last_sweep_time = time.time()
        self.last_loop_ms = 0.0

        # Create worker_telemetry table in rollup-db
        self.r_cur.execute("""
            CREATE TABLE IF NOT EXISTS worker_telemetry (
                worker_id int PRIMARY KEY,
                mode text NOT NULL,
                start_time timestamp NOT NULL,
                last_sweep timestamp NOT NULL,
                last_loop_ms numeric(10,2) NOT NULL DEFAULT 0.0,
                updated_at timestamp NOT NULL DEFAULT clock_timestamp()
            );
        """)
        # Initialize or update worker telemetry with actual process start time
        self.r_cur.execute("""
            INSERT INTO worker_telemetry (worker_id, mode, start_time, last_sweep, last_loop_ms, updated_at)
            VALUES (%s, %s, to_timestamp(%s), to_timestamp(%s), %s, clock_timestamp())
            ON CONFLICT (worker_id) DO UPDATE SET
                mode = EXCLUDED.mode,
                start_time = EXCLUDED.start_time,
                last_sweep = EXCLUDED.last_sweep,
                last_loop_ms = EXCLUDED.last_loop_ms,
                updated_at = clock_timestamp()
        """, (self.worker_id, self.mode, self.start_time, self.last_sweep_time, self.last_loop_ms))
        self.r_conn.commit()

        self.reconnect_backoff: Dict[int, float] = {}
        self.next_reconnect_time: Dict[int, float] = {}

        self._connect_all_tenants()

    def _connect_all_tenants(self):
        print(f"Establishing persistent connections to {len(self.owned_tenants)} tenant DBs...")
        t0 = time.time()
        for tid in self.owned_tenants:
            self._connect_tenant(tid)
        print(f"Connected to {len(self.tenant_conns)} tenant DBs in {time.time() - t0:.2f}s")

    def _connect_tenant(self, tid: int) -> bool:
        now = time.time()
        if now < self.next_reconnect_time.get(tid, 0.0):
            return False

        try:
            # Unregister old socket if any
            old_conn = self.tenant_conns.get(tid)
            if old_conn:
                try:
                    self.selector.unregister(old_conn.fileno())
                except Exception:
                    pass
                try: old_conn.close()
                except Exception: pass

            db_url = get_tenant_db_url(tid, self.profile)
            c = psycopg2.connect(db_url)
            c.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur = c.cursor()

            if self.mode == "event":
                cur.execute("LISTEN outbox")
                self.selector.register(c.fileno(), selectors.EVENT_READ, data=tid)

            self.tenant_conns[tid] = c
            self.tenant_curs[tid] = cur
            self.reconnect_backoff[tid] = 1.0
            self.next_reconnect_time.pop(tid, None)
            self.dirty_tenants.add(tid)
            print(f"[RECONNECT] Tenant {tid}: connected successfully (LISTEN outbox active).", flush=True)
            return True
        except Exception as e:
            curr_backoff = self.reconnect_backoff.get(tid, 1.0)
            self.next_reconnect_time[tid] = now + curr_backoff
            self.reconnect_backoff[tid] = min(30.0, curr_backoff * 2.0)
            print(f"[RECONNECT] Tenant {tid}: connection failed ({e}), next retry in {curr_backoff:.1f}s (backoff={self.reconnect_backoff[tid]:.1f}s)", flush=True)
            return False

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
        self.dirty_tenants.discard(tid)
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

    def process_tenant(self, tid: int, max_batch_size: int) -> Tuple[int, bool]:
        """Process one batch for a tenant. Returns (rows_ingested, has_more_pending)."""
        cursor_xid, cursor_id, snapshot_str, stalled = self._get_tenant_cursor_and_state(tid)
        if stalled:
            self.stalled_tenants.add(tid)
            self.dirty_tenants.discard(tid)
            return 0, False

        t_cur = self.tenant_curs.get(tid)
        if not t_cur or t_cur.closed:
            self._connect_tenant(tid)
            t_cur = self.tenant_curs.get(tid)
            if not t_cur:
                return 0, False

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
                max_batch_size
            ))
            rows = t_cur.fetchall()
        except Exception as query_err:
            print(f"[RECONNECT] Tenant {tid}: error during fetch ({query_err}). Reconnecting...", flush=True)
            self._connect_tenant(tid)
            return 0, False

        if not rows:
            return 0, False

        # In-memory folding for this tenant
        orders_folded: Dict[Tuple[int, datetime.datetime, str], Dict] = {}
        items_folded: Dict[Tuple[int, datetime.datetime, int, str], Dict] = {}
        quarantine_records = []

        last_xid = cursor_xid
        last_id = cursor_id

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

        # Check for further pending rows on this tenant using outbox enqueue time (created_at)
        try:
            t_cur.execute("""
                SELECT EXTRACT(EPOCH FROM (clock_timestamp() - created_at)), created_at
                FROM outbox
                WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
                  AND (xid > %s::xid8 OR (xid = %s::xid8 AND id > %s))
                ORDER BY xid ASC, id ASC
                LIMIT 1
            """, (last_xid, last_xid, last_id))
            pending_row = t_cur.fetchone()
            if pending_row:
                has_pending = True
                lag_sec = max(0.0, round(float(pending_row[0]), 3))
                oldest_ts = pending_row[1]
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
                    return len(rows), has_pending

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

            # PUSH: notify rollup_updated on rollup-db
            self.r_cur.execute("SELECT pg_notify('rollup_updated', %s)", (str(tid),))

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

            return len(rows), has_pending

        except Exception as apply_err:
            self.r_conn.rollback()
            self._record_tenant_stall(tid, str(apply_err))
            return 0, False

    def run_one_cycle(self) -> Dict[str, any]:
        t_cycle_start = time.perf_counter()
        tenants_with_activity = 0
        total_rows_ingested = 0

        if self.mode == "poll":
            active_list = [t for t in self.owned_tenants if t not in self.stalled_tenants]
        else:
            # Event mode: process only dirty tenants
            active_list = [t for t in list(self.dirty_tenants) if t not in self.stalled_tenants]

        is_single_active = (len(active_list) <= 1)

        for tid in active_list:
            # Deficit round-robin quota
            if is_single_active:
                batch_limit = self.per_tenant_cap
            else:
                self.deficit[tid] = min(self.deficit.get(tid, 0) + self.per_tenant_cap, 2000)
                batch_limit = min(self.deficit[tid], self.per_tenant_cap)

            rows_ingested, has_more = self.process_tenant(tid, batch_limit)

            if rows_ingested > 0:
                tenants_with_activity += 1
                total_rows_ingested += rows_ingested
                self.deficit[tid] = max(0, self.deficit.get(tid, 0) - rows_ingested)

            if not has_more:
                self.deficit[tid] = 0
                self.dirty_tenants.discard(tid)

        cycle_duration = time.perf_counter() - t_cycle_start
        return {
            "duration": cycle_duration,
            "active_tenants": tenants_with_activity,
            "rows_ingested": total_rows_ingested
        }

    def poll_notifications(self, timeout_sec: float):
        """Waits on sockets with selectors and collects dirty tenants."""
        if not self.tenant_conns:
            time.sleep(timeout_sec)
            return

        events = self.selector.select(timeout=timeout_sec)
        for key, mask in events:
            tid = key.data
            conn = self.tenant_conns.get(tid)
            if conn and not conn.closed:
                try:
                    conn.poll()
                    while conn.notifies:
                        n = conn.notifies.pop(0)
                        self.dirty_tenants.add(tid)

                    # Check for socket EOF (server connection termination)
                    import socket
                    s = socket.fromfd(conn.fileno(), socket.AF_INET, socket.SOCK_STREAM)
                    s.setblocking(False)
                    try:
                        is_eof = (s.recv(1, socket.MSG_PEEK) == b'')
                    except (BlockingIOError, InterruptedError):
                        is_eof = False
                    except Exception:
                        is_eof = True
                    finally:
                        s.close()

                    if is_eof:
                        print(f"[RECONNECT] Tenant {tid}: remote closed connection (EOF). Reconnecting...", flush=True)
                        self._connect_tenant(tid)
                except Exception as poll_err:
                    print(f"[RECONNECT] Tenant {tid}: connection error during poll ({poll_err}). Reconnecting...", flush=True)
                    self._connect_tenant(tid)

        # Coalesce bursts if any tenants became dirty (debounce)
        if self.dirty_tenants and self.debounce_ms > 0:
            burst_events = self.selector.select(timeout=self.debounce_ms / 1000.0)
            for key, mask in burst_events:
                tid = key.data
                conn = self.tenant_conns.get(tid)
                if conn and not conn.closed:
                    try:
                        conn.poll()
                        while conn.notifies:
                            n = conn.notifies.pop(0)
                            self.dirty_tenants.add(tid)
                    except Exception:
                        pass

    def update_idle_heartbeats(self):
        try:
            self.r_cur.execute("""
                UPDATE worker_state SET
                    heartbeat = clock_timestamp(),
                    lag_seconds = CASE WHEN has_pending AND oldest_pending_ts IS NOT NULL 
                                       THEN EXTRACT(EPOCH FROM (clock_timestamp() - oldest_pending_ts))
                                       ELSE 0.0 END
                WHERE tenant_id = ANY(%s) AND NOT stalled
            """, (self.owned_tenants,))

            self.r_cur.execute("""
                INSERT INTO worker_telemetry (worker_id, mode, start_time, last_sweep, last_loop_ms, updated_at)
                VALUES (%s, %s, to_timestamp(%s), to_timestamp(%s), %s, clock_timestamp())
                ON CONFLICT (worker_id) DO UPDATE SET
                    mode = EXCLUDED.mode,
                    last_sweep = EXCLUDED.last_sweep,
                    last_loop_ms = EXCLUDED.last_loop_ms,
                    updated_at = EXCLUDED.updated_at
            """, (self.worker_id, self.mode, self.start_time, self.last_sweep_time, self.last_loop_ms))
            self.r_conn.commit()
        except Exception:
            self.r_conn.rollback()

    def run_forever(self):
        print(f"[WORKER] Starting {self.mode.upper()} ingestion loop for {len(self.owned_tenants)} tenants...")
        last_sweep = time.time()
        last_hb = time.time()

        while True:
            try:
                now = time.time()
                # Periodic safety sweep (every 5.0s)
                if now - last_sweep >= self.safety_sweep_interval:
                    for tid in self.owned_tenants:
                        if tid not in self.stalled_tenants:
                            self.dirty_tenants.add(tid)
                    last_sweep = now
                    self.last_sweep_time = now

                if now - last_hb >= 2.0:
                    self.update_idle_heartbeats()
                    last_hb = now

                if self.mode == "event":
                    if not self.dirty_tenants:
                        # Wait for socket notifications up to safety sweep time
                        wait_time = max(0.01, min(0.5, self.safety_sweep_interval - (time.time() - last_sweep)))
                        self.poll_notifications(wait_time)

                    if self.dirty_tenants:
                        res = self.run_one_cycle()
                        self.last_loop_ms = round(res["duration"] * 1000.0, 2)
                else:
                    # Poll mode
                    res = self.run_one_cycle()
                    self.last_loop_ms = round(res["duration"] * 1000.0, 2)
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
        try: self.selector.close()
        except Exception: pass
        try:
            self.r_cur.close()
            self.r_conn.close()
        except Exception: pass
        for cur in self.tenant_curs.values():
            try: cur.close()
            except Exception: pass
        for conn in self.tenant_conns.values():
            try: conn.close()
            except Exception: pass

def benchmark_cycles(profile: str = "1m", tenant_counts: List[int] = [10, 50, 100], rounds: int = 50, mode: str = "event"):
    print("=" * 70)
    print(f"BENCHMARKING WORKER CYCLE TIME ({mode.upper()}) vs TENANTS (PROFILE: {profile.upper()})")
    print(f"Rounds: {rounds} per measurement")
    print("=" * 70)

    for n in tenant_counts:
        tids = list(range(1, n + 1))
        worker = MultiTenantWorker(profile=profile, target_tenants=tids, mode=mode, poll_interval=0.0)
        times = []
        for _ in range(rounds):
            res = worker.run_one_cycle()
            times.append(res["duration"] * 1000.0)  # ms
        worker.close()

        import numpy as np
        p50 = np.percentile(times, 50)
        p95 = np.percentile(times, 95)
        avg = np.mean(times)
        print(f"Mode: {mode:5s} | Tenants: {n:3d} | Avg Cycle: {avg:6.2f} ms | p50: {p50:6.2f} ms | p95: {p95:6.2f} ms")
    print("=" * 70)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-source multi-tenant rollup worker.")
    parser.add_argument("--profile", default=os.getenv("PROFILE", "1m"), choices=["1m", "10m"], help="Dataset profile")
    parser.add_argument("--mode", default=os.getenv("WORKER_MODE", "event"), choices=["event", "poll"], help="Worker execution mode")
    parser.add_argument("--worker-id", type=int, default=int(os.getenv("WORKER_ID", "0")), help="Worker ID for sharding")
    parser.add_argument("--worker-count", type=int, default=int(os.getenv("WORKER_COUNT", "1")), help="Total workers for sharding")
    parser.add_argument("--batch-cap", type=int, default=500, help="Max rows per tenant visit")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="Idle sleep interval (seconds, default: 500ms)")
    parser.add_argument("--debounce-ms", type=float, default=15.0, help="Debounce milliseconds")
    parser.add_argument("--benchmark-cycles", action="store_true", help="Run cycle time benchmark for 10, 50, 100 tenants")
    args = parser.parse_args()

    if args.benchmark_cycles:
        benchmark_cycles(profile=args.profile, mode=args.mode)
    else:
        w = MultiTenantWorker(
            profile=args.profile,
            mode=args.mode,
            worker_id=args.worker_id,
            worker_count=args.worker_count,
            per_tenant_cap=args.batch_cap,
            poll_interval=args.poll_interval,
            debounce_ms=args.debounce_ms
        )
        w.run_forever()
