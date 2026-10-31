#!/usr/bin/env python3
"""api/ui_proxy.py:
UI Proxy endpoints under /ui-api/:
Active ONLY when UI_OPEN_MODE=1 (default 1 for local demo).
Provides credential-free access for the dashboard by adding the appropriate
tenant key or admin key server-side. The browser never receives or sends any secret key.
Returns 404 on all routes when UI_OPEN_MODE=0.
"""
import os, time
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Path, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel

from config import get_config

# Startup warning
if os.getenv("UI_OPEN_MODE", "1") == "1":
    print("[WARNING] UI_OPEN_MODE is active: Dashboard UI proxy running at /ui-api without client credentials.")

def check_ui_open_mode():
    if os.getenv("UI_OPEN_MODE", "1") != "1":
        raise HTTPException(status_code=404, detail="UI Open Mode is disabled")

class TimedRoute(APIRoute):
    def get_route_handler(self):
        original_handler = super().get_route_handler()
        async def custom_handler(request: Request) -> Response:
            start_time = time.perf_counter()
            resp: Response = await original_handler(request)
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            resp.headers["X-Process-Time-ms"] = f"{duration_ms:.2f}"
            return resp
        return custom_handler

router = APIRouter(prefix="/ui-api", route_class=TimedRoute, dependencies=[Depends(check_ui_open_mode)])

def _get_admin_key() -> str:
    profile = os.getenv("PROFILE", "1m")
    cfg = get_config(profile)
    return cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")

def _get_tenant_key(tenant_id: int) -> str:
    import api.main
    key = api.main.tenant_registry_cache.get(tenant_id)
    if not key:
        key = f"tenant-key-{tenant_id}"
    return key

# Models
class DemoOrderReq(BaseModel):
    tenant_id: int = 1

class DemoFloodReq(BaseModel):
    tenant_id: int = 1
    n: int = 10

# -----------------------------------------------------------------------------
# Tenant Endpoints
# -----------------------------------------------------------------------------

@router.get("/tenants")
async def list_tenants():
    import api.platform
    pool = await api.platform._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT tenant_id, database_name FROM tenant_registry ORDER BY tenant_id ASC")
    return [{"tenant_id": r["tenant_id"], "name": f"Store {r['tenant_id']}", "database_name": r["database_name"]} for r in rows]

@router.get("/tenants/{tenant_id}/orders/count")
async def ui_get_orders_count(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to")
):
    import api.main
    key = _get_tenant_key(tenant_id)
    return await api.main.get_orders_count(
        tenant_id=tenant_id,
        from_time=from_time,
        to_time=to_time,
        x_tenant_key=key
    )

@router.get("/tenants/{tenant_id}/orders/trend")
async def ui_get_orders_trend(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    grain: Literal["hour", "day"] = Query("hour")
):
    import api.main
    key = _get_tenant_key(tenant_id)
    return await api.main.get_orders_trend(
        tenant_id=tenant_id,
        from_time=from_time,
        to_time=to_time,
        grain=grain,
        x_tenant_key=key
    )

@router.get("/tenants/{tenant_id}/products/top")
async def ui_get_top_products(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to"),
    n: int = Query(10, ge=1, le=100),
    by: Literal["sales", "units"] = Query("sales")
):
    import api.main
    key = _get_tenant_key(tenant_id)
    return await api.main.get_top_products(
        tenant_id=tenant_id,
        from_time=from_time,
        to_time=to_time,
        n=n,
        by=by,
        x_tenant_key=key
    )

@router.get("/tenants/{tenant_id}/orders/by-status")
async def ui_get_orders_by_status(
    tenant_id: int = Path(...),
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to")
):
    import api.main
    key = _get_tenant_key(tenant_id)
    return await api.main.get_orders_by_status(
        tenant_id=tenant_id,
        from_time=from_time,
        to_time=to_time,
        x_tenant_key=key
    )

@router.get("/tenants/{tenant_id}/stream")
async def ui_stream_events(tenant_id: int = Path(...)):
    import api.main
    key = _get_tenant_key(tenant_id)
    if hasattr(api.main, "stream_tenant_updates"):
        return await api.main.stream_tenant_updates(tenant_id=tenant_id, x_tenant_key=key)
    elif hasattr(api.main, "stream_events"):
        return await api.main.stream_events(tenant_id=tenant_id, x_tenant_key=key)
    raise HTTPException(status_code=501, detail="Stream endpoint not implemented on backend")

# -----------------------------------------------------------------------------
# Platform / Admin Endpoints
# -----------------------------------------------------------------------------

@router.get("/platform/summary")
async def ui_platform_summary(
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to")
):
    import api.platform
    adm_key = _get_admin_key()
    return await api.platform.platform_summary(from_time=from_time, to_time=to_time, x_admin_key=adm_key)

@router.get("/platform/tenants")
async def ui_platform_tenants(
    from_time: str = Query(..., alias="from"),
    to_time: str = Query(..., alias="to")
):
    import api.platform
    adm_key = _get_admin_key()
    return await api.platform.platform_tenants(from_time=from_time, to_time=to_time, x_admin_key=adm_key)

@router.get("/platform/scaling")
async def ui_platform_scaling():
    import api.platform
    adm_key = _get_admin_key()
    return await api.platform.platform_scaling(x_admin_key=adm_key)

@router.get("/monitor")
async def ui_admin_monitor():
    import api.platform
    adm_key = _get_admin_key()
    return await api.platform.admin_monitor(x_admin_key=adm_key)

@router.get("/stats")
async def ui_admin_stats():
    import api.main
    adm_key = _get_admin_key()
    return await api.main.admin_stats(x_admin_key=adm_key)

# -----------------------------------------------------------------------------
# Demo Endpoints
# -----------------------------------------------------------------------------

@router.get("/demo/status")
async def ui_demo_status():
    return {
        "demo_mode": os.getenv("DEMO_MODE") == "1",
        "worker_mode": os.getenv("WORKER_MODE", "event"),
        "profile": os.getenv("PROFILE", "1m")
    }

@router.post("/demo/order")
async def ui_demo_order(req: DemoOrderReq):
    import api.demo
    from api.demo import DemoOrderRequest
    adm_key = _get_admin_key()
    return await api.demo.demo_order(DemoOrderRequest(tenant_id=req.tenant_id), x_admin_key=adm_key)

@router.post("/demo/cancel")
async def ui_demo_cancel(req: DemoOrderReq):
    import api.demo
    from api.demo import DemoOrderRequest
    adm_key = _get_admin_key()
    return await api.demo.demo_cancel(DemoOrderRequest(tenant_id=req.tenant_id), x_admin_key=adm_key)

@router.post("/demo/flood")
async def ui_demo_flood(req: DemoFloodReq):
    import api.demo
    from api.demo import DemoFloodRequest
    adm_key = _get_admin_key()
    return await api.demo.demo_flood(DemoFloodRequest(tenant_id=req.tenant_id, n=req.n), x_admin_key=adm_key)

@router.get("/demo/flood/status")
async def ui_demo_flood_status():
    import api.demo
    adm_key = _get_admin_key()
    return await api.demo.demo_flood_status(x_admin_key=adm_key)

@router.post("/demo/flood/stop")
async def ui_demo_flood_stop():
    import api.demo
    adm_key = _get_admin_key()
    return await api.demo.demo_flood_stop(x_admin_key=adm_key)

@router.post("/demo/reset")
async def ui_demo_reset():
    import api.demo
    adm_key = _get_admin_key()
    return await api.demo.demo_reset(x_admin_key=adm_key)

@router.get("/demo/naive/{tenant_id}")
async def ui_demo_naive(tenant_id: int):
    import api.demo
    adm_key = _get_admin_key()
    return await api.demo.demo_naive(tenant_id, x_admin_key=adm_key)
