# CyberForge: Multi-Tenant Real-Time Aggregation Engine

CyberForge is a production-grade real-time analytical aggregation engine designed for multi-tenant e-commerce architectures. It provides strict physical and logical tenant data isolation (100 independent PostgreSQL tenant databases), statement-level outbox trigger capture, event-driven delta ingestion via PostgreSQL `LISTEN`/`NOTIFY`, and pre-aggregated central rollups capable of serving analytical dashboard queries in sub-10ms without querying tenant OLTP databases.

---

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
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

## One-Command Setup

The setup orchestrates container initialization, schema deployment, data splitting across 100 tenant databases, trigger installation, initial rollup population, and verification.

```powershell
# For 1M dataset profile:
py -3.12 scripts/setup.py --profile 1m

# For 10M dataset profile:
py -3.12 scripts/setup.py --profile 10m
```

---

## Start Commands

### 1. Start Infrastructure (PostgreSQL Containers)
```powershell
docker compose up -d
```
- Main OLTP database: `127.0.0.1:5432` (`shop`, `tenant_001` .. `tenant_100`)
- Central Rollup database: `127.0.0.1:5433` (`rollup_1m`, `rollup_10m`)

### 2. Start Multi-Tenant Worker Daemon
```powershell
py -3.12 worker/worker.py --profile 1m --mode event
```

### 3. Start Aggregation API Service
```powershell
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

*Data Source: `docs/results/scaling.json`*  
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

### 2. Worker Cost & Idle Footprint (100 Tenants)

| Metric | Measured Value |
|---|---|
| Event Mode Idle Loop p50 / p95 | 0.0 ms / 0.0 ms |
| Poll Mode Cycle Time Average | 216.1 ms |
| Main DB Idle Commits / sec | 30.15 |
| Rollup DB Idle Commits / sec | 1.75 |
| Idle CPU Usage (Cores) | 0.05 |

### 3. Scaling & Profile 10M Results

| Test Profile / Scenario | Metric | Result |
|---|---|---|
| Profile 10M Freshness | p50 / p95 / Max | 40.30 ms / 99.65 ms / 3228.03 ms |
| Flood Isolation (Tenant 1 flood) | Drain Time / Observer Latency | 0.24 s / 77.64 ms |

### 4. Database Safety Overhead (5 Repeats)

| Stage | Description | p50 (ms) | p95 (ms) | p99 (ms) | Mean (ms) | Writes / sec | Server Mean (ms) |
|---|---|---|---|---|---|---|---|
| `a` | Raw writes | 1.63 | 4.21 | 7.32 | 2.00 | 496.6 | 0.08 |
| `b` | Table with trigger | 1.54 | 3.09 | 6.10 | 1.72 | 575.3 | 0.06 |
| `c` | Trigger + outbox write | 1.39 | 2.93 | 4.33 | 1.54 | 642.9 | 0.05 |
| `d2` | Concurrency = 2 | 1.18 | 2.43 | 3.63 | 1.33 | 747.3 | 0.04 |
| `d8` | Concurrency = 8 | 1.31 | 2.66 | 4.14 | 1.47 | 675.4 | 0.05 |

---

## Known Limits

- DB safety failed organiser Good in poll mode on a single laptop, cause unproven; event mode not measured
- top-N growth 4.09x (Good only); count growth 1.78x
- B-3 limit 32 / 1 s queue never returned a 429 in a 200-client single-laptop test; neighbor p95 rose to 596 ms; cause unproven
- SSE max of 10 probes = 1.27 s, cause unproven
- one LISTEN connection per tenant DB, ceiling about max_connections (300), inferred not tested
- predefined queries only; B-4 not built; ranges snap to hour/day (UTC)
- synthetic dataset

---

## Dashboard

TODO after merge
