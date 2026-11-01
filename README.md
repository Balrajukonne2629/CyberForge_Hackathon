# CyberForge: Multi-Tenant Real-Time Aggregation Engine

CyberForge is a real-time analytical aggregation engine designed for multi-tenant e-commerce architectures. It provides strict physical and logical tenant data isolation (100 independent PostgreSQL tenant databases), statement-level outbox trigger capture, event-driven delta ingestion via PostgreSQL `LISTEN`/`NOTIFY`, and pre-aggregated central rollups capable of serving analytical dashboard queries in sub-10ms without querying tenant OLTP databases.

---

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [One-Command Setup](#one-command-setup)
- [Start Commands](#start-commands)
- [API Reference & curl Examples](#api-reference--curl-examples)
- [Architecture](#architecture)
- [Benchmark Results](#benchmark-results)
- [Known Limits](#known-limits)
- [Dashboard](#dashboard)

---

## Overview

CyberForge addresses the challenge of real-time analytical visibility across fragmented multi-tenant architectures without compromising OLTP database performance, tenant blast-radius containment, or transactional consistency.

See [PRD Specification](PRD.md) and [Presentation Deck](ppt.md) for full requirements traceability, per-tenant database architecture, and verified benchmarks.

*Tenant Model Note:* While the organiser specification defines 1M as 10 tenants and 10M as 100 tenants, CyberForge uses 100 dedicated tenant databases in both profiles (`tenant_001` .. `tenant_100`) to guarantee identical physical isolation, socket multiplexing, and fair-queuing behavior across scales without synthetic codebase branching.

Key capabilities:
- **Tenant Isolation**: 100 individual PostgreSQL databases (`tenant_001` .. `tenant_100`) on port 5432; no cross-tenant joins or queries.
- **Transactional Outbox Triggers**: Delta changes (`delta_count`, `delta_amount`) captured on `orders` and `order_items` via statement-level triggers with early-filtering of irrelevant mutations.
- **Event-Driven Streaming**: Dedicated `LISTEN outbox` notification channels enable immediate worker dispatch with 0ms idle loop overhead.
- **Central Rollups**: Central database (`rollup_1m` or `rollup_10m`) on port 5433 stores hourly order and daily product pre-aggregations.
- **Serving Layer**: FastAPI service serving pre-aggregated rollups, real-time Server-Sent Events (SSE), tenant concurrency limiting, and telemetry.

---

## Prerequisites

1. **Docker**: Docker Engine 24.0+ and Docker Compose v2 (e.g. Docker Desktop on Windows 11 with WSL2 engine enabled).
2. **Python**: Python 3.12 (standard virtual environment recommended).
3. **Dataset Path**: Ensure the dataset is accessible at either:
   - Archive: `data/mitigata_dataset.zip` (or pointed to via `$env:DATASET_ZIP`)
   - Directory: `data/mitigata_dataset/` containing `schema.sql`, `small/` (1M rows), and `main/` (10M rows).

---

## Quick Start

### 1. Clone & Environment Setup
```bash
git clone https://github.com/Balrajukonne2629/CyberForge_Hackathon.git
cd CyberForge_Hackathon

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Dataset Placement
Place the dataset in either of the following locations:
- Archive: `data/mitigata_dataset.zip` or set `DATASET_ZIP`
- Extracted directory: `data/mitigata_dataset/` containing `schema.sql`, `small/` (1M dataset), and `main/` (10M dataset).

---

## One-Command Setup

The setup orchestrates container initialization, schema deployment, data splitting across 100 tenant databases, trigger installation, initial rollup population, and verification.

```powershell
# For 1M dataset profile:
python scripts/setup.py --profile 1m

# For 10M dataset profile:
python scripts/setup.py --profile 10m
```

Clean-clone check (commit 182b518, 1M profile, one laptop i5-1235U 15.7 GB Windows 11): clone 2.7 s, setup 93.2 s, smoke 13/13 in 58.9 s, full suite 15/15 in 139 s, golden 480/480, tripwire 1,000,000 orders / 1,141,449 items. 10M full suite: rerun pending.

---

## Start Commands

### 1. Start Infrastructure (PostgreSQL Containers)
```powershell
docker compose up -d
```
- Main OLTP database: `127.0.0.1:5432` (`shop`, `tenant_001` .. `tenant_100`)
- Central Rollup database: `127.0.0.1:5433` (`rollup_1m`, `rollup_10m`)

### 2. Start Multi-Tenant Worker Daemon
Supports configuration via CLI flags or `PROFILE` and `WORKER_MODE` environment variables (`PROFILE` defaults to `1m`, `WORKER_MODE` defaults to `event`):

```powershell
# Using CLI flags:
py -3.12 worker/worker.py --profile 1m --mode event

# Or using environment variables:
$env:PROFILE="1m"
$env:WORKER_MODE="event"
py -3.12 worker/worker.py
```

### 3. Start Aggregation API Service
Supports configuration via `PROFILE` environment variable (`1m` or `10m`, default `1m`):

```powershell
# For 1M profile:
$env:PROFILE="1m"
py -3.12 -m uvicorn api.main:app --port 8000 --no-access-log

# For 10M profile:
$env:PROFILE="10m"
py -3.12 -m uvicorn api.main:app --port 8000 --no-access-log
```

---

## API Reference & curl Examples

Every tenant endpoint requires the `X-Tenant-Key` authentication header (`tenant-key-{tenant_id}`). Administrative endpoints require `X-Admin-Key` (`admin-secret-key-12345`).

### 1. Health Check
```bash
curl -s http://127.0.0.1:8000/health
```

### 2. Global Administrative Telemetry & Rollup Stats
```bash
curl -s http://127.0.0.1:8000/admin/stats \
  -H "X-Admin-Key: admin-secret-key-12345"
```

### 3. Single-Tenant or Global Rollup Rebuild
```bash
# Rebuild specific tenant:
curl -s -X POST "http://127.0.0.1:8000/admin/rebuild?tenant_id=1" \
  -H "X-Admin-Key: admin-secret-key-12345"

# Rebuild all 100 tenants:
curl -s -X POST "http://127.0.0.1:8000/admin/rebuild" \
  -H "X-Admin-Key: admin-secret-key-12345"
```

### 4. Admin Operational Telemetry (Worker Status & Latency Buffer)
```bash
curl -s http://127.0.0.1:8000/admin/monitor \
  -H "X-Admin-Key: admin-secret-key-12345"
```

### 5. Orders Count & Revenue
```bash
curl -s "http://127.0.0.1:8000/tenants/1/orders/count?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z" \
  -H "X-Tenant-Key: tenant-key-1"
```

### 6. Orders Trend by Time Grain
```bash
curl -s "http://127.0.0.1:8000/tenants/1/orders/trend?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z&grain=hour" \
  -H "X-Tenant-Key: tenant-key-1"
```

### 7. Orders Breakdown by Status
```bash
curl -s "http://127.0.0.1:8000/tenants/1/orders/by-status?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z" \
  -H "X-Tenant-Key: tenant-key-1"
```

### 8. Top Selling Products
```bash
curl -s "http://127.0.0.1:8000/tenants/1/products/top?from=2026-10-01T00:00:00Z&to=2026-10-30T00:00:00Z&n=5" \
  -H "X-Tenant-Key: tenant-key-1"
```

### 9. Real-Time Push Stream (Server-Sent Events)
```bash
curl -N -s "http://127.0.0.1:8000/tenants/1/stream" \
  -H "X-Tenant-Key: tenant-key-1"
```

---

## Architecture

1. **Per-Tenant OLTP Databases**: 100 isolated PostgreSQL tenant databases (`tenant_001` .. `tenant_100`) enforce strict physical blast radius containment.
2. **Statement-Level Triggers**: Transaction-safe statement-level triggers on `orders` and `order_items` intercept table mutations without row-by-row overhead.
3. **Early Filtering & Folding**: Non-financial mutations (e.g. `updated_at` only) are ignored, and within-bucket total changes fold into single net deltas.
4. **Local Outbox Tables**: Changes write to local tenant `outbox` records with explicit transaction identifiers (`xid`) for monotonic ordering.
5. **PostgreSQL LISTEN/NOTIFY Channels**: Committed transactions issue `pg_notify('outbox', tenant_id)` to instantly wake up the listening daemon.
6. **Multi-Tenant Rollup Worker**: A single event-driven Python daemon multiplexes 100 socket descriptors via `selectors` without thread pool overhead.
7. **Snapshot & Cursor Safety**: Rollup batches advance monotonically using `pg_snapshot_xmin` checks to handle out-of-order transaction commits safely.
8. **Central Rollup Database**: Central aggregation tables (`rollup_orders_hour`, `rollup_product_day`) maintain pre-computed sums with SQL check constraints.
9. **FastAPI Serving Layer**: The REST service queries only the central rollup database, isolating tenant transactional engines from analytical load.
10. **Telemetry & Concurrency Guard**: Includes per-tenant semaphore throttling, operational health monitoring, and real-time Server-Sent Events push streaming.

---

## Benchmark Results

*Data Source: `docs/results/scaling.json` and `docs/BENCHMARK.md`*  
*Hardware: Balu laptop i5-1235U, 15.7 GB | Data Size: 1M & 10M*  
*(Note: rerun pending on Lenovo)*

### 1. Ingestion Freshness by Tenant Count (Commit to Rollup Visibility)

| Tenants Connected | Mode | p50 Latency (ms) | p95 Latency (ms) |
|---|---|---|---|
| 1 Tenant | Event (`LISTEN`/`NOTIFY`) | 26.80 | 39.00 |
| 10 Tenants | Event (`LISTEN`/`NOTIFY`) | 27.50 | 39.00 |
| 50 Tenants | Event (`LISTEN`/`NOTIFY`) | 26.50 | 29.60 |
| 100 Tenants | Event (`LISTEN`/`NOTIFY`) | 26.60 | 39.30 |
| 1 Tenant | Poll Mode | 25.90 | 37.40 |
| 10 Tenants | Poll Mode | 29.40 | 38.20 |
| 50 Tenants | Poll Mode | 30.70 | 36.80 |
| 100 Tenants | Poll Mode | 27.60 | 43.90 |

For a single active tenant, freshness is similar in poll and event mode (p50 about 26 to 31 ms). The difference is worker cost: poll cycle averages 216.1 ms over 100 tenants at 1M; event mode does no work for idle tenants.

### 2. Query Latency and Growth

| Query Endpoint | 1M p95 Latency | 10M p95 Latency | Growth Ratio (p95) | PRD Tier (Latency) | PRD Tier (Growth) | Source File |
|---|---|---|---|---|---|---|
| `/orders/count` | 7.50 ms | 6.32 ms | 0.84x (~1x, within noise (10M faster than 1M is noise/cache, not a speedup)) | Great (< 200 ms) | Great (< 2x) | `docs/BENCHMARK.md` |
| `/orders/trend` (hour) | 13.58 ms | 10.74 ms | 0.79x (~1x, within noise (10M faster than 1M is noise/cache, not a speedup)) | Great (< 200 ms) | Great (< 2x) | `docs/BENCHMARK.md` |
| `/orders/by-status` | 9.83 ms | 6.09 ms | 0.62x (~1x, within noise (10M faster than 1M is noise/cache, not a speedup)) | Great (< 200 ms) | Great (< 2x) | `docs/BENCHMARK.md` |
| `/products/top` (sales) | 15.75 ms | 64.44 ms | 4.09x | Good (< 1 s) | Good (< 5x) | `docs/BENCHMARK.md` |
| Naive direct top-10 products (10M) | — | not in results file | — | — | — | `docs/BENCHMARK.md`* |

*Note: Raw per-run json not saved; values from the BENCHMARK.md run, one laptop, 1 run set. Old '1.78x/1.24x/1.13x' figures could not be reproduced and were removed.*  
*\*Note: `docs/BENCHMARK.md` Section 4.1 reports 113.55 ms execution time for direct unindexed heap scan on Tenant 1 rollup; ~1,047 ms raw direct OLTP query was referenced in preliminary test notes but is not in results file.*

### 3. Worker Cost & Idle Footprint (100 Tenants)

| Metric | Measured Value |
|---|---|
| Event Mode Idle Loop p50 / p95 | not measured |
| Poll Mode Cycle Time Average | 216.1 ms |
| Main DB Idle Commits / sec | 30.15 |
| Rollup DB Idle Commits / sec | 1.75 |
| Idle CPU Usage (Cores) | 0.05 |

### 4. Scaling & Profile 10M Results

| Test Scenario | Mode | Data Size | Metric | Result |
|---|---|---|---|---|
| Profile 10M Freshness | Event mode | 10M | p50 / p95 / Max | 40.30 ms / 99.65 ms / 3228.03 ms |
| Flood Isolation (Tenant 1 flood) | Event mode | 1M | Drain Time / Observer Latency | 0.24 s / 77.64 ms |

### 5. Write-path microbenchmark (no worker, no dashboard load)

Stages `a` through `d8` represent the 5 configurations from the benchmark protocol:
- `a`: Raw writes (baseline)
- `b`: Table with trigger enabled
- `c`: Trigger + outbox write
- `d2`: Concurrency = 2 (nominal 2 dashboard client load stage)
- `d8`: Concurrency = 8 (nominal 8 dashboard client load stage)

*Note: In this microbenchmark script, stages were executed sequentially on a single client connection without an active worker daemon or background dashboard load.*

| Stage | Description | p50 (ms) | p95 (ms) | p99 (ms) | Mean (ms) | Writes / sec | Server Mean (ms) |
|---|---|---|---|---|---|---|---|
| `a` | Raw writes | 1.63 | 4.21 | 7.32 | 2.00 | 496.6 | 0.08 |
| `b` | Table with trigger | 1.54 | 3.09 | 6.10 | 1.72 | 575.3 | 0.06 |
| `c` | Trigger + outbox write | 1.39 | 2.93 | 4.33 | 1.54 | 642.9 | 0.05 |
| `d2` | Concurrency = 2 | 1.18 | 2.43 | 3.63 | 1.33 | 747.3 | 0.04 |
| `d8` | Concurrency = 8 | 1.31 | 2.66 | 4.14 | 1.47 | 675.4 | 0.05 |

Later stages are faster than raw writes, so this reflects warm-up/order effects, not free triggers. This is NOT the PRD DB-safety test (with vs without our layer under load). Event-mode DB safety is not measured. Poll mode failed organiser Good (<25%) on this single laptop, cause unproven.

---

## Known Limits

- DB safety failed organiser Good in poll mode on a single laptop, cause unproven; event mode not measured
- top-N growth 4.09x (Good only); count growth 1.78x
- B-3 limit 32 / 1 s queue never returned a 429 in a 200-client single-laptop test; neighbor p95 rose to 596 ms; cause unproven
- SSE max of 10 probes = 1.27 s, cause unproven
- one LISTEN connection per tenant DB, ceiling about max_connections (300), inferred not tested
- predefined queries only; B-4 not built; ranges snap to hour/day (UTC)
- synthetic dataset
- correctness verified at 1m (smoke suite; full suite pending), 10m rerun pending on the Lenovo
- early filter drops only no-op updates (fuzz: 500 ops, 5 tenants, 0 mismatches at 1m)
- worker once died on "too many clients" at max_connections=100, now 300 plus per-tenant reconnect
- dashboard has no login by PRD design, the real API stays authenticated

---

## Dashboard

- **UI URL**: Access the interactive dashboard at http://localhost:8000/ui
- **Environment**:
  ```powershell
  $env:DEMO_MODE="1"
  $env:UI_OPEN_MODE="1"
  $env:PROFILE="1m"
  ```
- **Start Command**:
  ```powershell
  py -3.12 -m uvicorn api.main:app --port 8000 --no-access-log
  ```
- **Demo Orders**: Demo orders use IDs `900,000,000`–`949,999,999`. The Reset Demo button deletes only that range; real baseline data is untouched.
- **Authentication**: No login on the dashboard by PRD design (the server-side UI proxy injects authorization headers). The underlying API stays fully authenticated with `X-Tenant-Key` and `X-Admin-Key`. See [Dashboard API Reference](docs/DASHBOARD_API.md).
- **Time Ranges**: The **Live** range shows only demo orders generated in real time; the **Latest** range ends 30 Oct 2026 (the historical dataset upper bound).
