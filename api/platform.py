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

import api.main
from config import get_config
from api.demo import router as demo_router

router = APIRouter()
router.include_router(demo_router)

def _verify_admin(x_admin_key: Optional[str]):
    profile = os.getenv("PROFILE", "1m")
    cfg = get_config(profile)
    admin_key = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")
    if not x_admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden: Admin key required")

async def _get_pool():
    if api.main.pool is None:
        profile = os.getenv("PROFILE", "1m")
        cfg = get_config(profile)
        api.main.pool = await asyncpg.create_pool(cfg["database"]["rollup_db_url"], min_size=2, max_size=10)
    return api.main.pool

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
    f_dt = api.main.snap_to_hour(from_time, is_end=False)
    t_dt = api.main.snap_to_hour(to_time, is_end=True)
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

        # 5. Quarantine and worker stats
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
        "quarantine_count": int(quarantine_cnt or 0),
        "stalled_tenants_count": stalled_count,
        "max_lag_seconds": round(max_lag, 2),
        "range": {
            "from": f_dt.isoformat() + "Z",
            "to": t_dt.isoformat() + "Z"
        },
        "as_of": as_of
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
