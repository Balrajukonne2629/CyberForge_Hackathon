#!/usr/bin/env python3
"""api/demo.py:
Demo control endpoints:
Active only when DEMO_MODE=1 in environment and authenticated with X-Admin-Key.
Returns 404 when DEMO_MODE is off, and 403 when X-Admin-Key is missing or invalid.
"""
import os, datetime, time, asyncio, random
from typing import Optional, Dict
from fastapi import APIRouter, Header, HTTPException, Query, Path
from pydantic import BaseModel
import asyncpg

from config import get_config, get_tenant_db_url, get_all_tenant_ids

router = APIRouter()

# Global background flood tracking
_active_floods: Dict[int, asyncio.Task] = {}
_flood_stop_event = asyncio.Event()

def _check_demo_access(x_admin_key: Optional[str]):
    if os.getenv("DEMO_MODE") != "1":
        raise HTTPException(status_code=404, detail="Demo mode disabled")
    profile = os.getenv("PROFILE", "1m")
    cfg = get_config(profile)
    admin_key = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")
    if not x_admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden: Admin key required")

class DemoOrderRequest(BaseModel):
    tenant_id: int = 1

class DemoFloodRequest(BaseModel):
    tenant_id: int = 1
    n: int = 10

@router.get("/demo/keys")
async def demo_keys(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    profile = os.getenv("PROFILE", "1m")
    cfg = get_config(profile)
    admin_key = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")
    tenant_keys = {str(i): f"tenant-key-{i}" for i in range(1, 101)}
    return {
        "admin_key": admin_key,
        "admin_key_hint": admin_key,
        "tenant_keys": tenant_keys
    }

@router.post("/demo/order")
async def demo_order(
    req: DemoOrderRequest,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    profile = os.getenv("PROFILE", "1m")
    db_url = get_tenant_db_url(req.tenant_id, profile)

    conn = await asyncpg.connect(db_url)
    try:
        # Determine next demo order_id (in 900,000,000+ range)
        max_oid = await conn.fetchval("""
            SELECT COALESCE(MAX(order_id), 900000000)
            FROM orders
            WHERE order_id >= 900000000
        """)
        next_oid = max_oid + 1

        max_iid = await conn.fetchval("""
            SELECT COALESCE(MAX(item_id), 900000000)
            FROM order_items
            WHERE item_id >= 900000000
        """)
        next_iid = max_iid + 1

        # Fetch 1 to 3 products
        prod_rows = await conn.fetch("SELECT product_id, price FROM products LIMIT 5")
        if not prod_rows:
            raise HTTPException(status_code=500, detail="No products in tenant database")
        
        num_items = random.randint(1, 3)
        chosen = random.sample(prod_rows, min(num_items, len(prod_rows)))

        total = 0.0
        items_to_insert = []
        for idx, p in enumerate(chosen):
            qty = random.randint(1, 3)
            price = float(p["price"])
            total += qty * price
            items_to_insert.append((next_iid + idx, next_oid, req.tenant_id, p["product_id"], qty, price))

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        commit_ts = now_utc.isoformat()

        async with conn.transaction():
            await conn.execute("""
                INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                VALUES ($1, $2, 1, 'delivered', $3, $4, $4)
            """, next_oid, req.tenant_id, total, now_utc.replace(tzinfo=None))

            for item in items_to_insert:
                await conn.execute("""
                    INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, item[0], item[1], item[2], item[3], item[4], item[5])

        return {
            "status": "created",
            "tenant_id": req.tenant_id,
            "order_id": next_oid,
            "items_count": len(items_to_insert),
            "total": round(total, 2),
            "commit_ts": commit_ts
        }
    finally:
        await conn.close()

@router.post("/demo/cancel")
async def demo_cancel(
    req: DemoOrderRequest,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    profile = os.getenv("PROFILE", "1m")
    db_url = get_tenant_db_url(req.tenant_id, profile)

    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow("""
            SELECT order_id FROM orders
            WHERE tenant_id = $1 AND order_id >= 900000000 AND status != 'canceled'
            ORDER BY order_id DESC LIMIT 1
        """, req.tenant_id)

        if not row:
            return {"status": "no_demo_orders", "tenant_id": req.tenant_id}

        oid = row["order_id"]
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        commit_ts = now_utc.isoformat()

        await conn.execute("""
            UPDATE orders
            SET status = 'canceled', updated_at = $1
            WHERE order_id = $2
        """, now_utc.replace(tzinfo=None), oid)

        return {
            "status": "canceled",
            "tenant_id": req.tenant_id,
            "order_id": oid,
            "commit_ts": commit_ts
        }
    finally:
        await conn.close()

async def _flood_worker(tenant_id: int, n: int, stop_ev: asyncio.Event):
    profile = os.getenv("PROFILE", "1m")
    db_url = get_tenant_db_url(tenant_id, profile)
    conn = await asyncpg.connect(db_url)
    try:
        prod_rows = await conn.fetch("SELECT product_id, price FROM products LIMIT 5")
        if not prod_rows:
            return
        
        start_time = time.time()
        while not stop_ev.is_set() and (time.time() - start_time < 60.0):
            # Insert a batch of n orders per second, broken into sub-seconds
            step_orders = max(1, n // 5)
            for _ in range(step_orders):
                if stop_ev.is_set():
                    break
                max_oid = await conn.fetchval("""
                    SELECT COALESCE(MAX(order_id), 900000000)
                    FROM orders WHERE order_id >= 900000000
                """)
                next_oid = max_oid + 1
                max_iid = await conn.fetchval("""
                    SELECT COALESCE(MAX(item_id), 900000000)
                    FROM order_items WHERE item_id >= 900000000
                """)
                p = random.choice(prod_rows)
                qty = 1
                price = float(p["price"])
                total = qty * price
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                async with conn.transaction():
                    await conn.execute("""
                        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                        VALUES ($1, $2, 1, 'delivered', $3, $4, $4)
                    """, next_oid, tenant_id, total, now_utc.replace(tzinfo=None))
                    await conn.execute("""
                        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                        VALUES ($1, $2, $3, $4, $5, $6)
                    """, max_iid + 1, next_oid, tenant_id, p["product_id"], qty, price)
            await asyncio.sleep(0.2)
    except Exception:
        pass
    finally:
        await conn.close()

@router.post("/demo/flood")
async def demo_flood(
    req: DemoFloodRequest,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    global _flood_stop_event
    _flood_stop_event.clear()
    
    # Cancel previous task if running
    if req.tenant_id in _active_floods and not _active_floods[req.tenant_id].done():
        _active_floods[req.tenant_id].cancel()

    task = asyncio.create_task(_flood_worker(req.tenant_id, min(req.n, 100), _flood_stop_event))
    _active_floods[req.tenant_id] = task

    return {
        "status": "flood_started",
        "tenant_id": req.tenant_id,
        "orders_per_sec": min(req.n, 100),
        "max_duration_seconds": 60
    }

@router.post("/demo/flood/stop")
async def demo_flood_stop(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    global _flood_stop_event
    _flood_stop_event.set()
    for task in _active_floods.values():
        if not task.done():
            task.cancel()
    _active_floods.clear()
    return {"status": "flood_stopped"}

@router.get("/demo/naive/{tenant_id}")
async def demo_naive(
    tenant_id: int = Path(...),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    profile = os.getenv("PROFILE", "1m")
    db_url = get_tenant_db_url(tenant_id, profile)

    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute("SET statement_timeout = '5000ms'")
        now = datetime.datetime.now(datetime.timezone.utc)
        f_dt = (now - datetime.timedelta(days=30)).replace(tzinfo=None)
        t_dt = now.replace(tzinfo=None)

        t0 = time.perf_counter()
        rows = await conn.fetch("""
            SELECT 
                p.product_id,
                p.category,
                SUM(oi.qty)::bigint AS units,
                SUM(oi.qty * oi.price)::numeric(14,2) AS sales
            FROM orders o
            JOIN order_items oi ON o.order_id = oi.order_id
            JOIN products p ON oi.product_id = p.product_id
            WHERE o.tenant_id = $1 
              AND o.created_at >= $2 AND o.created_at < $3
              AND o.status NOT IN ('canceled', 'unavailable')
            GROUP BY p.product_id, p.category
            ORDER BY sales DESC
            LIMIT 10
        """, tenant_id, f_dt, t_dt)
        elapsed_ms = (time.perf_counter() - t0) * 1000

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
            "tenant_id": tenant_id,
            "elapsed_ms": round(elapsed_ms, 2),
            "rows_count": len(data),
            "rows": data
        }
    finally:
        await conn.close()

@router.post("/demo/reset")
async def demo_reset(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    global _flood_stop_event
    _flood_stop_event.set()

    profile = os.getenv("PROFILE", "1m")
    deleted_orders = 0
    deleted_items = 0

    all_tids = get_all_tenant_ids(100)
    for tid in all_tids:
        db_url = get_tenant_db_url(tid, profile)
        conn = None
        try:
            conn = await asyncpg.connect(db_url)
            del_items = await conn.execute("DELETE FROM order_items WHERE order_id >= 900000000")
            del_orders = await conn.execute("DELETE FROM orders WHERE order_id >= 900000000")
            # Parse count from 'DELETE <n>'
            if del_items.startswith("DELETE "):
                deleted_items += int(del_items.split(" ")[1])
            if del_orders.startswith("DELETE "):
                deleted_orders += int(del_orders.split(" ")[1])
        except Exception:
            pass
        finally:
            if conn:
                await conn.close()

    return {
        "status": "reset_complete",
        "deleted_orders": deleted_orders,
        "deleted_items": deleted_items
    }
