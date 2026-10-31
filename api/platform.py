#!/usr/bin/env python3
"""api/platform.py:
Platform aggregation and dashboard endpoints:
- GET /admin/platform/summary: cluster-wide totals, trend, top tenants, status split, quarantine count.
- GET /admin/platform/scaling: scaling test results from docs/results/scaling.json.
- GET /ui: serves the single-page dashboard HTML.
- Includes demo router.
"""
import os, datetime, json
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
import asyncpg

from config import get_config
from api.demo import router as demo_router
from api.ui_proxy import router as ui_proxy_router, TimedRoute

router = APIRouter(route_class=TimedRoute)
router.include_router(demo_router)
router.include_router(ui_proxy_router)

from api.main import snap_to_hour, snap_to_day

# Lazy pool reference to avoid circular import issues
_pool = None

def _verify_admin(x_admin_key: Optional[str]):
    profile = os.getenv("PROFILE", "1m")
    cfg = get_config(profile)
    admin_key = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")
    if not x_admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden: Admin key required")

async def _get_pool():
    global _pool
    import api.main
    if api.main.pool is not None:
        return api.main.pool
    if _pool is None:
        profile = os.getenv("PROFILE", "1m")
        cfg = get_config(profile)
        _pool = await asyncpg.create_pool(cfg["database"]["rollup_db_url"], min_size=2, max_size=10)
    return _pool

@router.get("/ui")
async def serve_ui():
    static_file = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if not os.path.exists(static_file):
        raise HTTPException(status_code=404, detail="Dashboard UI file not found")
    return FileResponse(static_file, media_type="text/html")

@router.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    full_path = os.path.join(os.path.dirname(__file__), "static", file_path)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(full_path)

@router.get("/admin/platform/summary")
async def platform_summary(
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _verify_admin(x_admin_key)
    f_dt = snap_to_hour(from_time, is_end=False)
    t_dt = snap_to_hour(to_time, is_end=True)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    as_of = now_utc.isoformat()

    pool = await _get_pool()
    async with pool.acquire() as conn:
        # 1. Totals across all tenants
        totals_row = await conn.fetchrow("""
            SELECT 
                COALESCE(SUM(order_count), 0)::bigint AS order_count,
                COALESCE(SUM(revenue), 0.00)::numeric(14,2) AS total_sales
            FROM rollup_orders_hour
            WHERE hour >= $1 AND hour < $2
        """, f_dt, t_dt)

        # 2. Hourly trend
        trend_rows = await conn.fetch("""
            SELECT 
                hour AS bucket,
                SUM(order_count)::bigint AS order_count,
                SUM(revenue)::numeric(14,2) AS sales
            FROM rollup_orders_hour
            WHERE hour >= $1 AND hour < $2
            GROUP BY hour
            HAVING SUM(order_count) > 0
            ORDER BY hour ASC
        """, f_dt, t_dt)

        # 3. Top 10 tenants by sales
        top_tenant_rows = await conn.fetch("""
            SELECT 
                tenant_id,
                SUM(order_count)::bigint AS order_count,
                SUM(revenue)::numeric(14,2) AS sales
            FROM rollup_orders_hour
            WHERE hour >= $1 AND hour < $2
            GROUP BY tenant_id
            ORDER BY sales DESC
            LIMIT 10
        """, f_dt, t_dt)

        # 4. Status split
        status_rows = await conn.fetch("""
            SELECT 
                status,
                SUM(order_count)::bigint AS order_count,
                SUM(revenue)::numeric(14,2) AS sales
            FROM rollup_orders_hour
            WHERE hour >= $1 AND hour < $2
            GROUP BY status
            HAVING SUM(order_count) > 0
            ORDER BY order_count DESC
        """, f_dt, t_dt)

        # 5. Top 10 products across all tenants
        f_day = snap_to_day(from_time, is_end=False)
        t_day = snap_to_day(to_time, is_end=True)
        top_product_rows = await conn.fetch("""
            SELECT 
                product_id,
                category,
                SUM(units)::bigint AS units,
                SUM(sales)::numeric(14,2) AS sales
            FROM rollup_product_day
            WHERE day >= $1 AND day < $2
            GROUP BY product_id, category
            HAVING SUM(units) > 0
            ORDER BY sales DESC
            LIMIT 10
        """, f_day, t_day)

        # 6. Quarantine and worker stats
        quarantine_cnt = await conn.fetchval("SELECT count(*) FROM quarantine")
        worker_rows = await conn.fetch("SELECT stalled, lag_seconds FROM worker_state")

    stalled_count = sum(1 for r in worker_rows if r["stalled"])
    max_lag = max([float(r["lag_seconds"] or 0.0) for r in worker_rows], default=0.0)

    return {
        "totals": {
            "order_count": int(totals_row["order_count"]),
            "total_sales": float(totals_row["total_sales"])
        },
        "trend": [
            {
                "bucket": r["bucket"].isoformat() + "Z",
                "order_count": int(r["order_count"]),
                "sales": float(r["sales"])
            }
            for r in trend_rows
        ],
        "top_tenants": [
            {
                "tenant_id": int(r["tenant_id"]),
                "order_count": int(r["order_count"]),
                "sales": float(r["sales"])
            }
            for r in top_tenant_rows
        ],
        "status_split": [
            {
                "status": r["status"],
                "order_count": int(r["order_count"]),
                "sales": float(r["sales"])
            }
            for r in status_rows
        ],
        "top_products": [
            {
                "product_id": int(r["product_id"]),
                "category": r["category"],
                "units": int(r["units"]),
                "sales": float(r["sales"])
            }
            for r in top_product_rows
        ],
        "quarantine_count": int(quarantine_cnt or 0),
        "stalled_tenants_count": stalled_count,
        "max_lag_seconds": round(max_lag, 2),
        "range": {
            "from": f_dt.isoformat() + "Z",
            "to": t_dt.isoformat() + "Z"
        },
        "as_of": as_of
    }

@router.get("/admin/platform/tenants")
async def platform_tenants(
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _verify_admin(x_admin_key)
    f_dt = snap_to_hour(from_time, is_end=False)
    t_dt = snap_to_hour(to_time, is_end=True)

    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                r.tenant_id,
                r.database_name,
                COALESCE(s.lag_seconds, 0.0) AS lag_seconds,
                COALESCE(s.stalled, false) AS stalled,
                COALESCE(o.order_count, 0)::bigint AS order_count,
                COALESCE(o.total_sales, 0.00)::numeric(14,2) AS total_sales,
                COALESCE(q.quarantine_count, 0)::bigint AS quarantine_count
            FROM tenant_registry r
            LEFT JOIN worker_state s ON r.tenant_id = s.tenant_id
            LEFT JOIN (
                SELECT tenant_id, SUM(order_count) AS order_count, SUM(revenue) AS total_sales
                FROM rollup_orders_hour
                WHERE hour >= $1 AND hour < $2
                GROUP BY tenant_id
            ) o ON r.tenant_id = o.tenant_id
            LEFT JOIN (
                SELECT tenant_id, COUNT(*) AS quarantine_count
                FROM quarantine
                GROUP BY tenant_id
            ) q ON r.tenant_id = q.tenant_id
            ORDER BY r.tenant_id ASC
        """, f_dt, t_dt)

    return [
        {
            "tenant_id": int(r["tenant_id"]),
            "database_name": r["database_name"],
            "lag_seconds": float(r["lag_seconds"]),
            "stalled": bool(r["stalled"]),
            "order_count": int(r["order_count"]),
            "sales": float(r["total_sales"]),
            "quarantine": int(r["quarantine_count"])
        }
        for r in rows
    ]

@router.get("/admin/monitor")
async def admin_monitor(x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")):
    _verify_admin(x_admin_key)
    profile = os.getenv("PROFILE", "1m")
    cfg = get_config(profile)
    w_cfg = cfg.get("worker", {})

    pool = await _get_pool()
    async with pool.acquire() as conn:
        tenant_rows = await conn.fetch("""
            SELECT 
                r.tenant_id,
                r.database_name,
                COALESCE(s.lag_seconds, 0.0) AS lag_seconds,
                COALESCE(s.stalled, false) AS stalled,
                COALESCE(s.cursor_xid::text, '') AS cursor_xid,
                COALESCE(q.quarantine_count, 0)::bigint AS quarantine_count
            FROM tenant_registry r
            LEFT JOIN worker_state s ON r.tenant_id = s.tenant_id
            LEFT JOIN (
                SELECT tenant_id, COUNT(*) AS quarantine_count
                FROM quarantine
                GROUP BY tenant_id
            ) q ON r.tenant_id = q.tenant_id
            ORDER BY r.tenant_id ASC
        """)
        quarantine_cnt = await conn.fetchval("SELECT count(*) FROM quarantine")
        
        # Real worker telemetry from worker_telemetry table
        telemetry_row = await conn.fetchrow("""
            SELECT mode, start_time, last_sweep, last_loop_ms, updated_at
            FROM worker_telemetry
            ORDER BY updated_at DESC
            LIMIT 1
        """)

    tenants_list = [
        {
            "tenant_id": int(r["tenant_id"]),
            "database_name": r["database_name"],
            "lag_seconds": round(float(r["lag_seconds"]), 2),
            "stalled": bool(r["stalled"]),
            "cursor_xid": str(r["cursor_xid"]),
            "quarantine_count": int(r["quarantine_count"])
        }
        for r in tenant_rows
    ]

    stalled_count = sum(1 for t in tenants_list if t["stalled"])
    all_lags = [t["lag_seconds"] for t in tenants_list] or [0.0]
    max_lag = max(all_lags, default=0.0)
    sorted_lags = sorted(all_lags)
    median_lag = sorted_lags[len(sorted_lags) // 2] if sorted_lags else 0.0
    worst_tenant = max(tenants_list, key=lambda t: t["lag_seconds"], default={"tenant_id": 1})

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_naive = now_utc.replace(tzinfo=None)

    worker_mode = "unknown"
    uptime_sec = None
    sweep_age_sec = None
    loop_time = None

    if telemetry_row:
        worker_mode = telemetry_row["mode"] or "unknown"
        if telemetry_row["start_time"]:
            uptime_sec = round((now_naive - telemetry_row["start_time"]).total_seconds(), 1)
        if telemetry_row["last_sweep"]:
            sweep_age_sec = round((now_naive - telemetry_row["last_sweep"]).total_seconds(), 1)
        if telemetry_row["last_loop_ms"] is not None:
            loop_time = round(float(telemetry_row["last_loop_ms"]), 2)

    changes_waiting = sum(1 for lag in all_lags if lag > 0.0)

    return {
        "worker_mode": worker_mode,
        "worker": {
            "mode": worker_mode,
            "uptime_seconds": uptime_sec,
            "safety_sweep_age_seconds": sweep_age_sec,
            "loop_time_ms": loop_time,
            "events_per_sec": None
        },
        "safety_sweep_interval": float(w_cfg.get("safety_sweep_interval", 5.0)),
        "debounce_ms": int(w_cfg.get("debounce_ms", 15)),
        "loop_time_ms": loop_time,
        "stalled_count": stalled_count,
        "max_lag_seconds": round(max_lag, 2),
        "median_lag_seconds": round(median_lag, 2),
        "worst_tenant_id": worst_tenant["tenant_id"],
        "changes_waiting": changes_waiting,
        "quarantine_count": int(quarantine_cnt or 0),
        "tenants": tenants_list,
        "as_of": now_utc.isoformat()
    }

@router.get("/admin/platform/scaling")
async def get_scaling_data(x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")):
    # Scaling metrics can be viewed by admin or public demo
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    json_path = os.path.join(base_dir, "docs", "results", "scaling.json")

    if not os.path.exists(json_path):
        return JSONResponse(status_code=404, content={"status": "not_measured", "message": "not measured yet"})

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # Transform/normalize if needed, or pass through directly
        poll_list = []
        event_list = []
        fresh = raw.get("freshness_by_tenants", {})
        for t, metrics in sorted(fresh.get("poll_mode", {}).items(), key=lambda x: int(x[0])):
            poll_list.append({"tenants": int(t), "p50_ms": metrics.get("p50_ms"), "p95_ms": metrics.get("p95_ms")})
        for t, metrics in sorted(fresh.get("event_mode", {}).items(), key=lambda x: int(x[0])):
            event_list.append({"tenants": int(t), "p50_ms": metrics.get("p50_ms"), "p95_ms": metrics.get("p95_ms")})

        normalized = {
            "date": raw.get("timestamp", "not measured yet"),
            "hardware": f"{raw.get('hardware', {}).get('cpus', '?')} CPUs, {raw.get('hardware', {}).get('os', '?')}, max_conn={raw.get('hardware', {}).get('max_connections_limit', '?')}",
            "poll": poll_list,
            "event": event_list,
            "idle": raw.get("idle_cost", {}),
            "raw": raw
        }
        return {"status": "available", "data": normalized}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
