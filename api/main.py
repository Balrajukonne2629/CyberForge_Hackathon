#!/usr/bin/env python3
"""FastAPI Dashboard Aggregation API:
Reads ONLY from rollup-db.
Enforces strict X-Tenant-Key authentication.
Enforces whole-hour and whole-day range snapping (UTC, from inclusive, to exclusive).
Echoes snapped range, as_of, lag_seconds, and worker_stalled on every response.
"""
import os, datetime, yaml
from contextlib import asynccontextmanager
from typing import Optional, Literal
from fastapi import FastAPI, Header, HTTPException, Query, Path
from pydantic import BaseModel
import asyncpg

from config import get_config

cfg = get_config()
ROLLUP_DB_URL = cfg["database"]["rollup_db_url"]
KEY_PREFIX = cfg.get("auth", {}).get("key_prefix", "tenant-key-")
ADMIN_KEY = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")

pool: Optional[asyncpg.Pool] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(ROLLUP_DB_URL, min_size=5, max_size=20)
    yield
    if pool:
        await pool.close()

app = FastAPI(title="CyberForge Multi-Tenant Aggregation Layer", lifespan=lifespan)

# Range snapping helpers
def snap_to_hour(dt_str: str, is_end: bool = False) -> datetime.datetime:
    # Accept ISO or standard formats
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
    expected_key = f"{KEY_PREFIX}{tenant_id}"
    if x_tenant_key != expected_key and x_tenant_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden: Tenant key mismatch")

async def get_system_metadata():
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT lag_seconds, worker_stalled, heartbeat, last_batch_at, has_pending, oldest_pending_ts
            FROM worker_state WHERE id = 1
        """)
        now = datetime.datetime.now(datetime.timezone.utc)
        as_of = now.isoformat()
        if row:
            lag = float(row["lag_seconds"])
            stalled = bool(row["worker_stalled"])
            heartbeat = row["heartbeat"]
            last_batch = row["last_batch_at"]
            has_pending = bool(row["has_pending"])
            oldest_pending = row["oldest_pending_ts"]

            hb_utc = heartbeat.replace(tzinfo=datetime.timezone.utc) if heartbeat and heartbeat.tzinfo is None else heartbeat
            hb_age = (now - hb_utc).total_seconds() if hb_utc else 999.0

            # If worker hasn't updated in > 2.0s (e.g. stopped)
            if hb_age > 2.0:
                if oldest_pending:
                    op_utc = oldest_pending.replace(tzinfo=datetime.timezone.utc) if oldest_pending.tzinfo is None else oldest_pending
                    lag = max(lag, round((now - op_utc).total_seconds(), 2))
                elif last_batch:
                    lb_utc = last_batch.replace(tzinfo=datetime.timezone.utc) if last_batch.tzinfo is None else last_batch
                    lag = max(lag, round((now - lb_utc).total_seconds(), 2))
                else:
                    lag = round(hb_age, 2)
            elif not has_pending:
                lag = 0.0

            return lag, stalled, as_of
        return 0.0, False, as_of

# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@app.get("/health")
async def health():
    lag, stalled, as_of = await get_system_metadata()
    return {
        "status": "healthy" if not stalled else "stalled",
        "worker_stalled": stalled,
        "lag_seconds": lag,
        "as_of": as_of
    }

@app.get("/tenants/{tenant_id}/orders/count")
async def get_orders_count(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    x_tenant_key: Optional[str] = Header(None, alias="X-Tenant-Key")
):
    verify_tenant_auth(tenant_id, x_tenant_key)
    f_dt = snap_to_hour(from_time, is_end=False)
    t_dt = snap_to_hour(to_time, is_end=True)
    lag, stalled, as_of = await get_system_metadata()

    assert pool is not None
    async with pool.acquire() as conn:
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
            ORDER BY 1 ASC
        """

    lag, stalled, as_of = await get_system_metadata()
    assert pool is not None
    async with pool.acquire() as conn:
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
    f_dt = snap_to_day(from_time, is_end=False)
    t_dt = snap_to_day(to_time, is_end=True)
    lag, stalled, as_of = await get_system_metadata()

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
        ORDER BY {order_by_col} DESC
        LIMIT $4
    """

    assert pool is not None
    async with pool.acquire() as conn:
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
    f_dt = snap_to_hour(from_time, is_end=False)
    t_dt = snap_to_hour(to_time, is_end=True)
    lag, stalled, as_of = await get_system_metadata()

    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                status,
                SUM(order_count)::bigint AS order_count,
                SUM(revenue)::numeric(14,2) AS sales
            FROM rollup_orders_hour
            WHERE tenant_id = $1 AND hour >= $2 AND hour < $3
            GROUP BY status
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
