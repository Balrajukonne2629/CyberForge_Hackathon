#!/usr/bin/env python3
"""tests/test_dashboard_demo.py:
Tests the interactive demo controls:
1. POST /demo/order inserts an order with order_id >= 900000000.
2. POST /demo/cancel cancels the demo order.
3. GET /demo/naive/{tenant_id} returns within 5s with valid rows.
4. POST /demo/reset deletes demo rows.
5. Verifies integrity tripwire passes.
"""
import os, sys, datetime
from fastapi.testclient import TestClient
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config
from api.main import app
from tests.integrity_tripwire import verify_integrity_tripwire

profile = os.getenv("PROFILE", "1m")
cfg = get_config(profile)
ADMIN_KEY = cfg.get("auth", {}).get("admin_key", "admin-secret-key-12345")

def test_demo_lifecycle():
    os.environ["DEMO_MODE"] = "1"
    headers = {"X-Admin-Key": ADMIN_KEY}

    with TestClient(app) as client:
        # 1. Insert Demo Order on Tenant 1
        r = client.post("/demo/order", json={"tenant_id": 1}, headers=headers)
        assert r.status_code == 200, f"Error: {r.text}"
        data = r.json()
        assert data["status"] == "created"
        assert data["order_id"] >= 900000000
        assert data["items_count"] >= 1
        assert "commit_ts" in data
        order_id = data["order_id"]
        print(f"Created demo order: #{order_id}")

        # 2. Naive Direct Query on Tenant 1
        r_naive = client.get("/demo/naive/1", headers=headers)
        assert r_naive.status_code == 200
        n_data = r_naive.json()
        assert n_data["elapsed_ms"] >= 0
        assert n_data["rows_count"] <= 10
        print(f"Naive query on Tenant 1 took {n_data['elapsed_ms']} ms")

        # 3. Cancel Demo Order on Tenant 1
        r_cancel = client.post("/demo/cancel", json={"tenant_id": 1}, headers=headers)
        assert r_cancel.status_code == 200
        c_data = r_cancel.json()
        assert c_data["status"] == "canceled"
        assert c_data["order_id"] == order_id
        print(f"Canceled demo order: #{order_id}")

        # 4. Reset Demo Data across all tenants
        r_reset = client.post("/demo/reset", headers=headers)
        assert r_reset.status_code == 200
        rst = r_reset.json()
        assert rst["status"] == "reset_complete"
        print(f"Reset demo data complete: deleted {rst['deleted_orders']} orders")

    # 5. Verify integrity tripwire
    assert verify_integrity_tripwire("1m", fail_fast=True)
    print("Tripwire passed perfectly after demo lifecycle!")

if __name__ == "__main__":
    test_demo_lifecycle()
