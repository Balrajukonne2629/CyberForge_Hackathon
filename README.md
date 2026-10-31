# CyberForge: Multi-Tenant Real-Time Aggregation Layer

Production-grade real-time analytical aggregation engine built for multi-tenant e-commerce systems. Enforces strict per-tenant database isolation (100 independent PostgreSQL databases), statement-level outbox capture, safe-xid event streaming, granular per-tenant fail-closed blast radius containment, and sub-15ms rollup querying across 1M and 10M events.

---

## Architecture Overview

```
[Store Front / OLTP Writes]
         │
         ▼
[100 Isolated Tenant DBs] (tenant_001 .. tenant_100 on Main DB, port 5432)
  ├── orders, order_items, customers, products
  └── outbox (Statement-level AFTER INSERT/UPDATE/DELETE triggers)
         │
         ▼ (safe-xid round-robin polling, snapshot-safe delta ingestion)
[Multi-Source Worker Daemon]
         │
         ▼ (UPSERT into rollup tables + per-tenant worker_state tracking)
[Rollup Database] (rollup_1m / rollup_10m on Rollup DB, port 5433)
  ├── rollup_orders_hour (CHECK order_count >= 0)
  ├── rollup_product_day (CHECK units >= 0)
  ├── worker_state (cursor_xid, lag_seconds, stalled, heartbeat)
  └── quarantine (isolated payload dead-letter store)
         │
         ▼ (Zero-touch to tenant DBs, snapped UTC windows)
[FastAPI Aggregation Service] (port 8000)
  └── /health, /admin/stats, /admin/rebuild, /metrics/orders/hourly, /metrics/products/daily
```

---

## Prerequisites

1. **Operating System**: Windows 11 with WSL2 backend.
2. **Docker Desktop**: Running with the WSL2 engine enabled (`docker info` succeeds).
3. **Python**: Python 3.10 to 3.12 (Python 3.12 recommended).
4. **Git**: Installed and available in PATH.
5. **Hardware**: 16 GB RAM (WSL2 allocated 12 GB), 20+ GB free SSD storage.

---

## Quick Start (PowerShell)

### 1. Clone & Environment Setup

```powershell
# Clone the repository
git clone https://github.com/Balrajukonne2629/CyberForge_Hackathon.git cyberforge
cd cyberforge

# Create and activate Python virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install pinned dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Dataset Placement

Ensure the `mitigata_dataset` folder or archive is present:
- Option A: Place `mitigata_dataset.zip` directly at `data/mitigata_dataset.zip`
- Option B: Set `$env:DATASET_ZIP = "C:\path\to\mitigata_dataset.zip"`
- Option C: Extract `mitigata_dataset.zip` so `data/mitigata_dataset/` contains `schema.sql`, `small/`, and `main/`.

### 3. One-Command Setup (1M Profile)

```powershell
# Runs pre-flight checks, starts Docker containers, splits 100 tenant DBs,
# installs triggers, builds rollups, verifies completeness, and tests API.
python scripts/setup.py --profile 1m
```

---

## Running the System

### Start the Multi-Tenant Worker Daemon
```powershell
python worker/worker.py --profile 1m
```

### Start the FastAPI Aggregation API
```powershell
python -m uvicorn api.main:app --port 8000
```

Verify API health:
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"
```

---

## Running Verification & Tests

### 1. Unified Trust & Measurement Test Suite (All 11 Suites)
```powershell
python tests/run_all.py --profile 1m
```
Suites executed:
1. Tenant Isolation
2. Idempotency per-tenant
3. Out-of-Order Commit per-tenant
4. Single-Tenant Mutation & Fan-out
5. Concurrent Multi-Tenant Updates (25 DBs)
6. Fairness & Anti-Starvation
7. Quarantine Handling
8. Crash Recovery per-tenant
9. Per-Tenant Fail Closed & Stall Isolation
10. Rebuild Equality under Load per-tenant
11. Golden Ground-Truth Parity vs Tenant DBs

### 2. Dataset Integrity Tripwire
```powershell
python tests/integrity_tripwire.py 1m
```

### 3. Fail-Closed Blast Radius Demo
```powershell
python tests/demo_worker_stalled.py
```

### 4. Official Benchmark Harness
```powershell
python bench/bench.py --profile 1m
```

---

## Resetting Everything

To stop all services and wipe container volumes for a completely clean slate:
```powershell
docker compose down -v
```
To purge extracted temporary staging files if needed:
```powershell
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue data/.split_staging_*
```
