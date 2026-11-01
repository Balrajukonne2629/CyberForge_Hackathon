#!/usr/bin/env python3
"""api/main.py:
FastAPI Multi-Tenant Aggregation API (v1.1):
- Reads ONLY from rollup-db.
- Enforces strict tenant authentication against tenant_registry in rollup-db.
- Enforces whole-hour and whole-day range snapping (UTC, from inclusive, to exclusive).
- Echoes snapped range, as_of, lag_seconds, and worker_stalled specifically for that tenant.
- /health reports global system status, count of stalled tenants, and max lag.
- /admin/stats reports per-tenant lag, stalled flags, and connection metadata.
- /admin/rebuild supports single-tenant or full-cluster parallel rebuild.
"""
import os, datetime, asyncio, time
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional, Literal, Dict
from fastapi import FastAPI, Header, HTTPException, Query, Path
from fastapi.responses import StreamingResponse
import asyncpg, json, numpy as np

from config import get_config

profile = os.getenv("PROFILE", "1m")
cfg = get_config(profile)
ROLLUP_DB_URL = cfg["database"]["rollup_db_url"]
ADMIN_KEY = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")

# B-3 Concurrency Limit configuration
PER_TENANT_LIMIT = int(cfg.get("concurrency", {}).get("per_tenant_limit", 32))
QUEUE_TIMEOUT_SEC = float(cfg.get("concurrency", {}).get("queue_timeout_seconds", 1.0))
tenant_semaphores: Dict[int, asyncio.Semaphore] = {}

# B-5 Telemetry Ring Buffer (last 60s) and Request Counters
metrics_ring_buffer = deque(maxlen=20000)
request_counters = {
    "total_requests": 0,
    "count_429": 0
}

def record_request_metric(endpoint: str, latency_ms: float, is_429: bool = False):
    now = time.time()
    request_counters["total_requests"] += 1
    if is_429:
        request_counters["count_429"] += 1
    metrics_ring_buffer.append((now, endpoint, latency_ms, is_429))

@asynccontextmanager
async def tenant_concurrency_guard(tenant_id: int, endpoint: str):
    if tenant_id not in tenant_semaphores:
        tenant_semaphores[tenant_id] = asyncio.Semaphore(PER_TENANT_LIMIT)
    sem = tenant_semaphores[tenant_id]

    t0 = time.perf_counter()
    acquired = False
    try:
        if QUEUE_TIMEOUT_SEC <= 0.0:
            if sem.locked():
                raise asyncio.TimeoutError()
            await sem.acquire()
        else:
            await asyncio.wait_for(sem.acquire(), timeout=QUEUE_TIMEOUT_SEC)
        acquired = True
    except asyncio.TimeoutError:
        record_request_metric(endpoint, (time.perf_counter() - t0) * 1000.0, is_429=True)
        raise HTTPException(
            status_code=429,
            detail="Too Many Requests: per-tenant concurrency limit exceeded",
            headers={"Retry-After": "1"}
        )

    try:
        yield
    finally:
        if acquired:
            sem.release()
        record_request_metric(endpoint, (time.perf_counter() - t0) * 1000.0, is_429=False)

pool: Optional[asyncpg.Pool] = None
tenant_registry_cache: Dict[int, str] = {}

async def load_tenant_registry():
    global tenant_registry_cache
    if not pool:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT tenant_id, api_key FROM tenant_registry")
        tenant_registry_cache = {r["tenant_id"]: r["api_key"] for r in rows}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(ROLLUP_DB_URL, min_size=20, max_size=50)
    await load_tenant_registry()
    yield
    if pool:
        await pool.close()

app = FastAPI(title="CyberForge Multi-Tenant Aggregation Layer", lifespan=lifespan)

# Range snapping helpers
def snap_to_hour(dt_str: str, is_end: bool = False) -> datetime.datetime:
    cleaned = dt_str.replace("Z", "+00:00").replace("T", " ")
    dt = datetime.datetime.fromisoformat(cleaned)
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    snapped = dt.replace(minute=0, second=0, microsecond=0)
    if is_end and (dt.minute > 0 or dt.second > 0 or dt.microsecond > 0):
        snapped += datetime.timedelta(hours=1)
    return snapped

def snap_to_day(dt_str: str, is_end: bool = False) -> datetime.datetime:
    cleaned = dt_str.replace("Z", "+00:00").replace("T", " ")
    dt = datetime.datetime.fromisoformat(cleaned)
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    snapped = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if is_end and (dt.hour > 0 or dt.minute > 0 or dt.second > 0 or dt.microsecond > 0):
        snapped += datetime.timedelta(days=1)
    return snapped

def verify_tenant_auth(tenant_id: int, x_tenant_key: Optional[str]):
    if not x_tenant_key:
        raise HTTPException(status_code=403, detail="Missing X-Tenant-Key header")
    if x_tenant_key == ADMIN_KEY:
        return
    expected_key = tenant_registry_cache.get(tenant_id) or f"tenant-key-{tenant_id}"
    if x_tenant_key != expected_key:
        raise HTTPException(status_code=403, detail="Forbidden: Tenant key mismatch")

def verify_admin_auth(x_admin_key: Optional[str]):
    if not x_admin_key or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid Admin Key")

async def get_tenant_metadata(tenant_id: int, conn: Optional[asyncpg.Connection] = None):
    assert pool is not None
    now = datetime.datetime.now(datetime.timezone.utc)
    as_of = now.isoformat()

    if conn is not None:
        row = await conn.fetchrow("""
            SELECT lag_seconds, stalled, heartbeat, last_batch_at, has_pending, oldest_pending_ts
            FROM worker_state
            WHERE tenant_id = $1
        """, tenant_id)
    else:
        async with pool.acquire() as c:
            row = await c.fetchrow("""
                SELECT lag_seconds, stalled, heartbeat, last_batch_at, has_pending, oldest_pending_ts
                FROM worker_state
                WHERE tenant_id = $1
            """, tenant_id)

    if not row:
        return 0.0, False, as_of

    lag = float(row["lag_seconds"] or 0.0)
    stalled = bool(row["stalled"])
    has_pending = bool(row["has_pending"])
    oldest_pending = row["oldest_pending_ts"]

    # lag_seconds = now minus ENQUEUE time (outbox.created_at) of oldest pending row, 0 if none
    if not has_pending or oldest_pending is None:
        lag = 0.0
    else:
        op_utc = oldest_pending.replace(tzinfo=datetime.timezone.utc) if oldest_pending.tzinfo is None else oldest_pending
        lag = max(0.0, round((now - op_utc).total_seconds(), 3))

    return lag, stalled, as_of

# -----------------------------------------------------------------------------
# System & Monitoring Endpoints
# -----------------------------------------------------------------------------

@app.get("/health")
async def health():
    assert pool is not None
    now = datetime.datetime.now(datetime.timezone.utc)
    as_of = now.isoformat()

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tenant_id, lag_seconds, stalled, heartbeat
            FROM worker_state
        """)

    if not rows:
        return {
            "status": "healthy",
            "worker_stalled": False,
            "total_tenants": 0,
            "stalled_tenants_count": 0,
            "stalled_tenant_ids": [],
            "max_lag_seconds": 0.0,
            "as_of": as_of
        }

    stalled_ids = [r["tenant_id"] for r in rows if r["stalled"]]
    max_lag = max([float(r["lag_seconds"] or 0.0) for r in rows], default=0.0)
    is_stalled = len(stalled_ids) > 0

    return {
        "status": "healthy" if not is_stalled else "stalled",
        "worker_stalled": is_stalled,
        "total_tenants": len(rows),
        "stalled_tenants_count": len(stalled_ids),
        "stalled_tenant_ids": stalled_ids,
        "max_lag_seconds": round(max_lag, 2),
        "as_of": as_of
    }

@app.get("/admin/stats")
async def admin_stats(x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")):
    verify_admin_auth(x_admin_key)
    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.tenant_id, r.database_name, s.lag_seconds, s.stalled, s.last_error, s.heartbeat
            FROM tenant_registry r
            LEFT JOIN worker_state s ON r.tenant_id = s.tenant_id
            ORDER BY r.tenant_id ASC
        """)

    stalled_count = sum(1 for r in rows if r["stalled"])
    max_lag = max([float(r["lag_seconds"] or 0.0) for r in rows], default=0.0)
    tenants_data = {
        str(r["tenant_id"]): {
            "database_name": r["database_name"],
            "lag_seconds": float(r["lag_seconds"] or 0.0),
            "stalled": bool(r["stalled"]),
            "last_error": r["last_error"],
            "heartbeat": r["heartbeat"].isoformat() if r["heartbeat"] else None
        }
        for r in rows
    }

    return {
        "total_tenants": len(rows),
        "stalled_count": stalled_count,
        "max_lag_seconds": round(max_lag, 2),
        "tenants": tenants_data
    }

@app.post("/admin/rebuild")
async def admin_rebuild(
    tenant_id: Optional[int] = Query(None),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    verify_admin_auth(x_admin_key)
    from scripts.rebuild import rebuild_tenant, rebuild_all
    prof = cfg.get("active_profile", "1m")
    loop = asyncio.get_running_loop()

    if tenant_id is not None:
        res = await loop.run_in_executor(None, rebuild_tenant, tenant_id, prof)
        return {"status": "success", "scope": f"tenant_{tenant_id}", "result": res}
    else:
        elapsed = await loop.run_in_executor(None, rebuild_all, prof)
        return {"status": "success", "scope": "all_tenants", "elapsed_seconds": round(elapsed, 2)}

# -----------------------------------------------------------------------------
# B-5 Admin Monitoring Endpoint
# -----------------------------------------------------------------------------

@app.get("/admin/monitor")
async def get_admin_monitor(x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")):
    verify_admin_auth(x_admin_key)
    assert pool is not None
    now_ts = time.time()

    async with pool.acquire() as conn:
        tenant_rows = await conn.fetch("""
            SELECT 
                w.tenant_id, 
                w.lag_seconds, 
                w.stalled, 
                w.cursor_xid::text as cursor_xid, 
                w.cursor_id,
                coalesce(q.q_cnt, 0) as quarantine_count
            FROM worker_state w
            LEFT JOIN (
                SELECT tenant_id, count(*) as q_cnt 
                FROM quarantine 
                GROUP BY tenant_id
            ) q ON q.tenant_id = w.tenant_id
            ORDER BY w.tenant_id
        """)

        w_row = await conn.fetchrow("""
            SELECT mode, start_time, last_sweep, last_loop_ms, updated_at 
            FROM worker_telemetry 
            WHERE worker_id = 0
        """)

    cutoff = now_ts - 60.0
    recent = [m for m in metrics_ring_buffer if m[0] >= cutoff]
    endpoint_data = {}
    for ep in ["/orders/count", "/orders/trend", "/orders/by-status", "/products/top"]:
        lats_200 = [m[2] for m in recent if m[1] == ep and not m[3]]
        calls_429 = len([m for m in recent if m[1] == ep and m[3]])
        calls_200 = len(lats_200)
        calls_total = calls_200 + calls_429
        if lats_200:
            endpoint_data[ep] = {
                "p50_ms": round(float(np.percentile(lats_200, 50)), 2),
                "p95_ms": round(float(np.percentile(lats_200, 95)), 2),
                "calls_200": calls_200,
                "calls_429": calls_429,
                "calls_60s": calls_total
            }
        else:
            endpoint_data[ep] = {
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "calls_200": 0,
                "calls_429": calls_429,
                "calls_60s": calls_total
            }

    def to_utc_ts(dt: Optional[datetime.datetime]) -> Optional[float]:
        if not dt:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        return dt.timestamp()

    if w_row:
        up_ts = to_utc_ts(w_row["updated_at"])
        st_ts = to_utc_ts(w_row["start_time"])
        sw_ts = to_utc_ts(w_row["last_sweep"])
        hb_age = max(0.0, now_ts - up_ts) if up_ts else 999.0
        worker_alive = (hb_age <= 15.0)
        uptime = max(0.0, round(now_ts - st_ts, 1)) if (worker_alive and st_ts) else None
        sweep_age = max(0.0, round(now_ts - sw_ts, 2)) if sw_ts else 0.0
        loop_ms = float(w_row["last_loop_ms"] or 0.0)
        mode = w_row["mode"]
    else:
        worker_alive = False
        uptime = None
        sweep_age = 0.0
        loop_ms = 0.0
        mode = cfg.get("worker", {}).get("mode", "event")

    return {
        "tenants": [
            {
                "tenant_id": r["tenant_id"],
                "lag_seconds": float(r["lag_seconds"] or 0.0),
                "stalled": bool(r["stalled"]),
                "cursor_xid": r["cursor_xid"],
                "cursor_id": r["cursor_id"],
                "quarantine_count": int(r["quarantine_count"])
            }
            for r in tenant_rows
        ],
        "endpoints": endpoint_data,
        "worker": {
            "worker_alive": worker_alive,
            "mode": mode,
            "uptime_seconds": uptime,
            "safety_sweep_age_seconds": sweep_age,
            "loop_time_ms": loop_ms
        },
        "requests": {
            "total_requests": request_counters["total_requests"],
            "count_429": request_counters["count_429"]
        }
    }

# -----------------------------------------------------------------------------
# Tenant Dashboard Endpoints (Reads ONLY from Rollup DB)
# -----------------------------------------------------------------------------

@app.get("/tenants/{tenant_id}/orders/count")
async def get_orders_count(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    x_tenant_key: Optional[str] = Header(None, alias="X-Tenant-Key")
):
    verify_tenant_auth(tenant_id, x_tenant_key)
    async with tenant_concurrency_guard(tenant_id, "/orders/count"):
        f_dt = snap_to_hour(from_time, is_end=False)
        t_dt = snap_to_hour(to_time, is_end=True)

        assert pool is not None
        async with pool.acquire() as conn:
            lag, stalled, as_of = await get_tenant_metadata(tenant_id, conn=conn)
            row = await conn.fetchrow("""
                SELECT 
                    COALESCE(SUM(order_count), 0)::bigint AS order_count,
                    COALESCE(SUM(revenue), 0.00)::numeric(14,2) AS total_sales
                FROM rollup_orders_hour
                WHERE tenant_id = $1 AND hour >= $2 AND hour < $3
            """, tenant_id, f_dt, t_dt)

        return {
            "data": {
                "order_count": int(row["order_count"]),
                "total_sales": float(row["total_sales"])
            },
            "range": {
                "from": f_dt.isoformat() + "Z",
                "to": t_dt.isoformat() + "Z"
            },
            "as_of": as_of,
            "lag_seconds": lag,
            "worker_stalled": stalled
        }

@app.get("/tenants/{tenant_id}/orders/trend")
async def get_orders_trend(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    grain: Literal["hour", "day"] = Query("hour"),
    x_tenant_key: Optional[str] = Header(None, alias="X-Tenant-Key")
):
    verify_tenant_auth(tenant_id, x_tenant_key)
    async with tenant_concurrency_guard(tenant_id, "/orders/trend"):
        if grain == "hour":
            f_dt = snap_to_hour(from_time, is_end=False)
            t_dt = snap_to_hour(to_time, is_end=True)
            query = """
                SELECT 
                    hour AS bucket,
                    SUM(order_count)::bigint AS order_count,
                    SUM(revenue)::numeric(14,2) AS sales
                FROM rollup_orders_hour
                WHERE tenant_id = $1 AND hour >= $2 AND hour < $3
                GROUP BY hour
                HAVING SUM(order_count) > 0
                ORDER BY hour ASC
            """
        else:
            f_dt = snap_to_day(from_time, is_end=False)
            t_dt = snap_to_day(to_time, is_end=True)
            query = """
                SELECT 
                    DATE_TRUNC('day', hour) AS bucket,
                    SUM(order_count)::bigint AS order_count,
                    SUM(revenue)::numeric(14,2) AS sales
                FROM rollup_orders_hour
                WHERE tenant_id = $1 AND hour >= $2 AND hour < $3
                GROUP BY 1
                HAVING SUM(order_count) > 0
                ORDER BY 1 ASC
            """

        assert pool is not None
        async with pool.acquire() as conn:
            lag, stalled, as_of = await get_tenant_metadata(tenant_id, conn=conn)
            rows = await conn.fetch(query, tenant_id, f_dt, t_dt)

        data = [
            {
                "bucket": r["bucket"].isoformat() + "Z",
                "order_count": int(r["order_count"]),
                "sales": float(r["sales"])
            }
            for r in rows
        ]

        return {
            "data": data,
            "range": {
                "from": f_dt.isoformat() + "Z",
                "to": t_dt.isoformat() + "Z"
            },
            "as_of": as_of,
            "lag_seconds": lag,
            "worker_stalled": stalled
        }

@app.get("/tenants/{tenant_id}/products/top")
async def get_top_products(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    n: int = Query(10, ge=1, le=100),
    by: Literal["sales", "units"] = Query("sales"),
    x_tenant_key: Optional[str] = Header(None, alias="X-Tenant-Key")
):
    verify_tenant_auth(tenant_id, x_tenant_key)
    async with tenant_concurrency_guard(tenant_id, "/products/top"):
        f_dt = snap_to_day(from_time, is_end=False)
        t_dt = snap_to_day(to_time, is_end=True)
        order_by_col = "sales" if by == "sales" else "units"
        query = f"""
            SELECT 
                product_id,
                category,
                SUM(units)::bigint AS units,
                SUM(sales)::numeric(14,2) AS sales
            FROM rollup_product_day
            WHERE tenant_id = $1 AND day >= $2 AND day < $3
            GROUP BY product_id, category
            HAVING SUM(units) > 0
            ORDER BY {order_by_col} DESC
            LIMIT $4
        """

        assert pool is not None
        async with pool.acquire() as conn:
            lag, stalled, as_of = await get_tenant_metadata(tenant_id, conn=conn)
            rows = await conn.fetch(query, tenant_id, f_dt, t_dt, n)

        data = [
            {
                "product_id": int(r["product_id"]),
                "category": r["category"],
                "units": int(r["units"]),
                "sales": float(r["sales"])
            }
            for r in rows
        ]

        return {
            "data": data,
            "range": {
                "from": f_dt.isoformat() + "Z",
                "to": t_dt.isoformat() + "Z"
            },
            "as_of": as_of,
            "lag_seconds": lag,
            "worker_stalled": stalled
        }

@app.get("/tenants/{tenant_id}/orders/by-status")
async def get_orders_by_status(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    x_tenant_key: Optional[str] = Header(None, alias="X-Tenant-Key")
):
    verify_tenant_auth(tenant_id, x_tenant_key)
    async with tenant_concurrency_guard(tenant_id, "/orders/by-status"):
        f_dt = snap_to_hour(from_time, is_end=False)
        t_dt = snap_to_hour(to_time, is_end=True)

        assert pool is not None
        async with pool.acquire() as conn:
            lag, stalled, as_of = await get_tenant_metadata(tenant_id, conn=conn)
            rows = await conn.fetch("""
                SELECT 
                    status,
                    SUM(order_count)::bigint AS order_count,
                    SUM(revenue)::numeric(14,2) AS sales
                FROM rollup_orders_hour
                WHERE tenant_id = $1 AND hour >= $2 AND hour < $3
                GROUP BY status
                HAVING SUM(order_count) > 0
                ORDER BY order_count DESC
            """, tenant_id, f_dt, t_dt)

        data = [
            {
                "status": r["status"],
                "order_count": int(r["order_count"]),
                "sales": float(r["sales"])
            }
            for r in rows
        ]

        return {
            "data": data,
            "range": {
                "from": f_dt.isoformat() + "Z",
                "to": t_dt.isoformat() + "Z"
            },
            "as_of": as_of,
            "lag_seconds": lag,
            "worker_stalled": stalled
        }

@app.get("/tenants/{tenant_id}/stream")
async def stream_tenant_updates(
    tenant_id: int = Path(...),
    x_tenant_key: Optional[str] = Header(None, alias="X-Tenant-Key")
):
    verify_tenant_auth(tenant_id, x_tenant_key)
    assert pool is not None

    async def event_generator():
        async with pool.acquire() as conn:
            queue: asyncio.Queue = asyncio.Queue(maxsize=100)

            def on_notify(connection, pid, channel, payload):
                if payload == str(tenant_id):
                    try:
                        queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        pass

            await conn.add_listener("rollup_updated", on_notify)
            try:
                # Send immediate initial state
                lag, stalled, as_of = await get_tenant_metadata(tenant_id)
                bucket_rows = await conn.fetch("""
                    SELECT hour, status, order_count, revenue
                    FROM rollup_orders_hour
                    WHERE tenant_id = $1
                    ORDER BY hour DESC
                    LIMIT 5
                """, tenant_id)
                buckets = [
                    {"hour": r["hour"].isoformat(), "status": r["status"], "count": r["order_count"], "revenue": float(r["revenue"])}
                    for r in bucket_rows
                ]
                init_msg = json.dumps({
                    "as_of": as_of,
                    "lag_seconds": lag,
                    "worker_stalled": stalled,
                    "changed_buckets": buckets
                })
                yield f"data: {init_msg}\n\n"

                # Stream updates on NOTIFY
                while True:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=15.0)
                        while not queue.empty():
                            queue.get_nowait()

                        lag, stalled, as_of = await get_tenant_metadata(tenant_id)
                        bucket_rows = await conn.fetch("""
                            SELECT hour, status, order_count, revenue
                            FROM rollup_orders_hour
                            WHERE tenant_id = $1
                            ORDER BY hour DESC
                            LIMIT 5
                        """, tenant_id)
                        buckets = [
                            {"hour": r["hour"].isoformat(), "status": r["status"], "count": r["order_count"], "revenue": float(r["revenue"])}
                            for r in bucket_rows
                        ]
                        msg = json.dumps({
                            "as_of": as_of,
                            "lag_seconds": lag,
                            "worker_stalled": stalled,
                            "changed_buckets": buckets
                        })
                        yield f"data: {msg}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                await conn.remove_listener("rollup_updated", on_notify)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

try:
    from api.platform import router as platform_router
    app.include_router(platform_router)
except ImportError:
    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
