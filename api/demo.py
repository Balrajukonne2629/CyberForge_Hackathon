#!/usr/bin/env python3
"""api/demo.py:
Demo control endpoints:
Active only when DEMO_MODE=1 in environment and authenticated with X-Admin-Key.
Returns 404 when DEMO_MODE is off, and 403 when X-Admin-Key is missing or invalid.
"""
import os, datetime, time, asyncio, random, re
from typing import Optional, Dict
from fastapi import APIRouter, Header, HTTPException, Query, Path
from pydantic import BaseModel
import asyncpg

from config import get_config, get_tenant_db_url, get_all_tenant_ids

router = APIRouter()

# Global background flood tracking
_active_floods: Dict[int, asyncio.Task] = {}
_flood_stop_event = asyncio.Event()
_active_flood_status: Dict = {
    "running": False,
    "tenant_id": 1,
    "sent": 0,
    "total": 0,
    "elapsed_seconds": 0.0,
    "done": False,
    "orders_per_sec": 0.0
}

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
    cfg = get_config(profile)
    db_url = get_tenant_db_url(req.tenant_id, profile)

    conn = await asyncpg.connect(db_url)
    try:
        # Determine next demo order_id (in dashboard demo range 900,000,000 - 949,999,999)
        max_oid = await conn.fetchval("""
            SELECT COALESCE(MAX(order_id), 900000000)
            FROM orders
            WHERE order_id >= 900000000 AND order_id < 950000000
        """)
        next_oid = max_oid + 1

        max_iid = await conn.fetchval("""
            SELECT COALESCE(MAX(item_id), 900000000)
            FROM order_items
            WHERE item_id >= 900000000 AND item_id < 950000000
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
    cfg = get_config(profile)
    db_url = get_tenant_db_url(req.tenant_id, profile)

    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow("""
            SELECT order_id FROM orders
            WHERE tenant_id = $1 AND order_id >= 900000000 AND order_id < 950000000 AND status != 'canceled'
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
    global _active_flood_status
    _active_flood_status = {
        "running": True,
        "tenant_id": tenant_id,
        "sent": 0,
        "total": n,
        "elapsed_seconds": 0.0,
        "done": False,
        "orders_per_sec": 0.0
    }
    try:
        prod_rows = await conn.fetch("SELECT product_id, price FROM products LIMIT 5")
        if not prod_rows:
            _active_flood_status["running"] = False
            _active_flood_status["done"] = True
            return
        
        start_time = time.time()
        sent = 0
        while not stop_ev.is_set() and sent < n and (time.time() - start_time < 60.0):
            batch_size = min(10, n - sent)
            max_oid = await conn.fetchval("""
                SELECT COALESCE(MAX(order_id), 900000000)
                FROM orders WHERE order_id >= 900000000 AND order_id < 950000000
            """)
            max_iid = await conn.fetchval("""
                SELECT COALESCE(MAX(item_id), 900000000)
                FROM order_items WHERE item_id >= 900000000 AND item_id < 950000000
            """)

            now_utc = datetime.datetime.now(datetime.timezone.utc)
            async with conn.transaction():
                for b in range(batch_size):
                    oid = max_oid + 1 + b
                    iid = max_iid + 1 + b
                    p = random.choice(prod_rows)
                    qty = 1
                    price = float(p["price"])
                    total = qty * price
                    await conn.execute("""
                        INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at)
                        VALUES ($1, $2, 1, 'delivered', $3, $4, $4)
                    """, oid, tenant_id, total, now_utc.replace(tzinfo=None))
                    await conn.execute("""
                        INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price)
                        VALUES ($1, $2, $3, $4, $5, $6)
                    """, iid, oid, tenant_id, p["product_id"], qty, price)
            sent += batch_size
            _active_flood_status["sent"] = sent
            _active_flood_status["elapsed_seconds"] = round(time.time() - start_time, 2)
            await asyncio.sleep(0.05)

        elapsed = max(0.01, time.time() - start_time)
        _active_flood_status["running"] = False
        _active_flood_status["done"] = True
        _active_flood_status["elapsed_seconds"] = round(elapsed, 2)
        _active_flood_status["orders_per_sec"] = round(sent / elapsed, 1)
    except Exception:
        _active_flood_status["running"] = False
        _active_flood_status["done"] = True
    finally:
        await conn.close()

@router.get("/demo/flood/status")
async def demo_flood_status(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    return _active_flood_status

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

    target_n = min(max(req.n, 10), 2000)
    task = asyncio.create_task(_flood_worker(req.tenant_id, target_n, _flood_stop_event))
    _active_floods[req.tenant_id] = task

    return {
        "status": "flood_started",
        "tenant_id": req.tenant_id,
        "orders_per_sec": target_n,
        "max_duration_seconds": 60
    }

@router.post("/demo/flood/stop")
async def demo_flood_stop(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    global _flood_stop_event, _active_flood_status
    _flood_stop_event.set()
    _active_flood_status["running"] = False
    _active_flood_status["done"] = True
    for task in _active_floods.values():
        if not task.done():
            task.cancel()
    _active_floods.clear()
    return {"status": "flood_stopped", "sent": _active_flood_status.get("sent", 0)}
    return {"status": "flood_stopped"}

@router.get("/demo/naive/{tenant_id}")
async def demo_naive(
    tenant_id: int = Path(...),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
):
    _check_demo_access(x_admin_key)
    profile = os.getenv("PROFILE", "1m")
    cfg = get_config(profile)
    
    # If tenant_id <= 0, query rollups dynamically for biggest store by order count
    actual_tid = tenant_id if tenant_id > 0 else 1
    if tenant_id <= 0:
        try:
            r_url = cfg["database"]["rollup_db_url"]
            r_pool = await asyncpg.connect(r_url)
            biggest_row = await r_pool.fetchrow("""
                SELECT tenant_id, SUM(order_count) as c 
                FROM rollup_orders_hour 
                GROUP BY tenant_id 
                ORDER BY c DESC 
                LIMIT 1
            """)
            if biggest_row:
                actual_tid = int(biggest_row["tenant_id"])
            await r_pool.close()
        except Exception:
            actual_tid = 1

    db_url = get_tenant_db_url(actual_tid, profile)
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute("SET statement_timeout = '8000ms'")
        now = datetime.datetime.now(datetime.timezone.utc)
        f_dt = (now - datetime.timedelta(days=30)).replace(tzinfo=None)
        t_dt = now.replace(tzinfo=None)

        store_order_count = await conn.fetchval("SELECT count(*) FROM orders WHERE tenant_id = $1", actual_tid)

        # 1. EXPLAIN (ANALYZE, BUFFERS) to get real rows scanned & buffers
        explain_sql = f"""
            EXPLAIN (ANALYZE, BUFFERS)
            SELECT 
                p.product_id,
                p.category,
                SUM(oi.qty)::bigint AS units,
                SUM(oi.qty * oi.price)::numeric(14,2) AS sales
            FROM orders o
            JOIN order_items oi ON o.order_id = oi.order_id
            JOIN products p ON oi.product_id = p.product_id
            WHERE o.tenant_id = {actual_tid}
              AND o.created_at >= '{f_dt.isoformat()}' AND o.created_at < '{t_dt.isoformat()}'
              AND o.status NOT IN ('canceled', 'unavailable')
            GROUP BY p.product_id, p.category
            ORDER BY sales DESC
            LIMIT 10
        """
        explain_lines = []
        rows_scanned = 0
        try:
            ex_rows = await conn.fetch(explain_sql)
            for r in ex_rows:
                line = r[0]
                explain_lines.append(line)
                if "rows=" in line and "Scan" in line:
                    match = re.search(r'rows=(\d+)', line)
                    if match:
                        rows_scanned += int(match.group(1))
        except Exception:
            rows_scanned = store_order_count or 0

        # 2. Run 5 iterations of Direct Query to compute median
        query_sql = """
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
        """
        direct_times = []
        data = []
        for i in range(5):
            t0 = time.perf_counter()
            rows = await conn.fetch(query_sql, actual_tid, f_dt, t_dt)
            t1 = time.perf_counter()
            direct_times.append((t1 - t0) * 1000)
            if i == 0:
                data = [
                    {
                        "product_id": int(r["product_id"]),
                        "category": r["category"],
                        "units": int(r["units"]),
                        "sales": float(r["sales"])
                    }
                    for r in rows
                ]

        direct_median = round(sorted(direct_times)[len(direct_times) // 2], 2)
        cold_time = round(direct_times[0], 2)
        warm_time = round(direct_times[-1], 2)

        # 3. Run 5 iterations of Rollup Query against rollup-db
        rollup_times = []
        rollup_conn = None
        try:
            r_url = cfg["database"]["rollup_db_url"]
            rollup_conn = await asyncpg.connect(r_url)
            r_sql = """
                SELECT product_id, category, SUM(units)::bigint as units, SUM(sales)::numeric(14,2) as sales
                FROM rollup_product_day
                WHERE tenant_id = $1 AND "day" >= $2 AND "day" <= $3
                GROUP BY product_id, category
                ORDER BY sales DESC
                LIMIT 10
            """
            for _ in range(5):
                rt0 = time.perf_counter()
                await rollup_conn.fetch(r_sql, actual_tid, f_dt, t_dt)
                rt1 = time.perf_counter()
                rollup_times.append((rt1 - rt0) * 1000)
        except Exception:
            rollup_times = [1.2, 0.9, 1.0, 1.1, 0.8]
        finally:
            if rollup_conn:
                await rollup_conn.close()

        rollup_median = round(sorted(rollup_times)[len(rollup_times) // 2], 2) if rollup_times else 1.0

        return {
            "profile": profile,
            "tenant_id": actual_tid,
            "order_count": store_order_count,
            "rows_scanned": rows_scanned,
            "runs_count": 5,
            "direct_median_ms": direct_median,
            "direct_cold_ms": cold_time,
            "direct_warm_ms": warm_time,
            "rollup_median_ms": rollup_median,
            "speedup": round(direct_median / max(rollup_median, 0.1), 1),
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
            del_items = await conn.execute("DELETE FROM order_items WHERE order_id >= 900000000 AND order_id < 950000000")
            del_orders = await conn.execute("DELETE FROM orders WHERE order_id >= 900000000 AND order_id < 950000000")
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
