# PRD: Multi-Tenant Real-Time Aggregation Layer (CyberForge)

**Event:** Mitigata CyberForge Hackathon Finals (24-Hour Hackathon)  
**Problem Statement:** Problem #1 — Fast Multi-Tenant Data Aggregation  
**Team:** Balu & Vignesh Thati (Team `nextgen`)  
**Version:** 2.0 (Final Official Submission)  
**Status:** Frozen (10M full-suite rerun pending)  
**File Location:** `PRD.md` (Repository Root)  

---

## 1. Executive Summary

CyberForge is a real-time analytical aggregation engine designed for multi-tenant e-commerce architectures. In high-volume multi-tenant systems, executing analytical aggregation queries directly against operational transactional (OLTP) tables degrades checkout latency, creates noisy-neighbor outages, and compromises tenant blast-radius containment.

CyberForge solves this by decoupling high-volume transactional OLTP databases from the analytical serving tier:
1. **Physical Isolation**: 100 independent PostgreSQL databases (`tenant_001` .. `tenant_100` on port 5432).
2. **Transactional Outbox Capture**: Statement-level `AFTER` triggers with lossless early-filtering capture changes directly into local outbox tables with monotonic `xid8` ordering.
3. **Event-Driven Multiplexing**: A single lightweight Python worker multiplexes 100 database sockets via OS `selectors` and PostgreSQL `LISTEN`/`NOTIFY`, eliminating idle polling overhead (designed for minimal idle wait, see `docs/ARCHITECTURE_SCALING.md`).
4. **Central Rollup Tier**: Pre-aggregates hourly order metrics (`rollup_orders_hour`) and daily product metrics (`rollup_product_day`) on port 5433, serving queries in sub-10ms without touching tenant OLTP databases.
5. **Fail-Closed Blast Radius Containment**: If a data corruption or constraint violation occurs for tenant *T*, only tenant *T* stalls (`worker_state.stalled = true`); all other 99 tenants continue ingesting in real time.

---

## 2. Requirements & Deliverables Matrix

### Core Functional Requirements (FR-1 through FR-7)

| Requirement ID | Specification Description | Technical Implementation | Verified Performance | Status |
|---|---|---|---|---|
| **FR-1** | Order counts and revenue totals for any arbitrary date range | `GET /tenants/{id}/orders/count?from=...&to=...` | **6.32 ms p95** at 10M (Great: <200ms) | **PASS** |
| **FR-2** | Historical trend over time (hourly or daily bucket grain) | `GET /tenants/{id}/orders/trend?from=...&to=...&grain=hour` | **10.74 ms p95** at 10M (Great: <200ms) | **PASS** |
| **FR-3** | Top-N selling products ranked by sales or units | `GET /tenants/{id}/products/top?from=...&to=...&n=5` | **64.44 ms p95** at 10M (Covering Index) | **PASS (Great: <200ms)** |
| **FR-4** | Categorical breakdown (order volume and revenue by status) | `GET /tenants/{id}/orders/by-status?from=...&to=...` | **6.09 ms p95** at 10M across 8 status codes | **PASS** |
| **FR-5** | Strict tenant isolation (tenants must never see cross-tenant data) | Physical separation across 100 databases + `X-Tenant-Key` auth | 0 cross-tenant joins or shared table space | **PASS** |
| **FR-6** | Immediate visibility of new data and mutations (placed to canceled) | Statement triggers + local outbox + `LISTEN/NOTIFY` CDC | **26.6 ms p50, 39.3 ms p95** freshness lag | **PASS** |
| **FR-7** | Documented REST API + Interactive Web Interface | FastAPI OpenAPI documentation (`/docs`) + Zero-Login Dashboard (`/ui`) | Complete curl runbook and UI | **PASS** |

### Phase 3 Enterprise Bonuses (B-1 through B-5)

| Bonus ID | Capability Description | Technical Implementation | Verification Proof | Status |
|---|---|---|---|---|
| **B-1** | Response Data Freshness Indicator (plus SSE stream) | Every tenant endpoint returns `lag_seconds`, `as_of`, and `worker_stalled`. Extra: `GET /tenants/{id}/stream` (SSE push) | Verified `lag_seconds` / `as_of` in responses; SSE p50 = 40.8 ms, p95 = 49.4 ms | **PASS (Great)** |
| **B-2** | Zero-Downtime Rollup Rebuild (Single-Tenant & Global) | `POST /admin/rebuild?tenant_id={id}` shadow table swap | Snapshot-isolated atomic swap with 0 dropped events | **PASS** |
| **B-3** | Per-Tenant Concurrency Throttling & Queue Guard | Per-tenant semaphores (limit 32 concurrent queries, 1.0s timeout) | Returns `429 Retry-After: 1`; neighbor latency stays <75ms | **PASS** |
| **B-4** | Dynamic Metric Extension via Config | Deliberately omitted to prevent dynamic SQL evaluation overhead | Architectural tradeoff documented | **N/A** |
| **B-5** | Real-Time Operational Monitoring & Health Matrix | `GET /admin/monitor` returning latency buffer & tenant status | 60s ring buffer, separates 429s from 500s | **PASS** |

---

## 3. System Architecture & Contracts

### 3.1 Physical Storage Layout (Port 5432)
- **Container**: `cyberforgeproject-main-db` running PostgreSQL 16.
- **Tenant Databases**: 100 dedicated databases (`tenant_001` through `tenant_100` for 1M; `t10m_001` through `t10m_100` for 10M).
- **Tenant Schema**: `tenants`, `customers`, `products`, `orders`, `order_items`.
- **Invariant**: The `tenant_id` column is preserved in all tenant tables, designed so there is zero code branching between single-tenant and multi-tenant testing suites.
- **Capacity**: PostgreSQL `max_connections` configured to `300` to support concurrent persistent listeners across all databases.

### 3.2 Central Rollup Database (Port 5433)
- **Container**: `cyberforgeproject-rollup-db` (`rollup_1m` / `rollup_10m`).
- **Pre-Aggregated Rollups**:
  - `rollup_orders_hour`: `(tenant_id, hour, status) -> (order_count, total_amount)`
  - `rollup_product_day`: `(tenant_id, day, product_id, category) -> (units, sales)`
- **Composite Covering Index**:
  ```sql
  CREATE INDEX idx_rollup_prod_covering 
  ON rollup_product_day (tenant_id, day) 
  INCLUDE (product_id, units, sales);
  ```
  Reduces 30-day top-N buffer page reads by 97.9% (from 26,783 to 572 buffers), enabling 64ms p95 at 10M scale.
- **Metadata Tables**:
  - `tenant_registry`: `(tenant_id, database_name, api_key, created_at)`
  - `worker_state`: `(tenant_id, cursor_xid, cursor_id, snapshot, lag_seconds, heartbeat, stalled, last_error, has_pending, oldest_pending_ts)`
  - `quarantine`: Isolates corrupted or unparseable payloads with specific error reasons.

### 3.3 Statement-Level Outbox & Watermark Monotonicity
- Statement-level `AFTER INSERT OR UPDATE OR DELETE` triggers on `orders` and `order_items` capture signed deltas (+1 / -1) with early filtering of no-op mutations (e.g. `updated_at` only), eliminating redundant outbox evaluations (measured at 39.7% reduction on 1,000-op workload in `docs/BENCHMARK.md`).
- **Safe-XID Monotonic Watermark**:
  ```sql
  WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
  ORDER BY xid ASC, id ASC
  ```
  Transactions that commit out of order are never skipped or lost.

### 3.4 Per-Tenant Blast Radius Isolation
- Rollup tables enforce database engine constraints: `CHECK (order_count >= 0)` and `CHECK (units >= 0)`.
- If an inconsistent delta arrives for tenant *T*:
  1. Transaction on `rollup-db` is rolled back *only* for tenant *T*.
  2. Worker sets `worker_state.stalled = true` and logs `last_error` for tenant *T*.
  3. **Ingestion halts only for tenant *T*. Ingestion for the remaining 99 tenants continues without interruption.**
  4. API queries for tenant *T* echo `"worker_stalled": true`, preventing silent data drift.

---

## 4. Empirical Performance Benchmarks

Measured on standard commodity laptop hardware (Intel Core i5-1235U, 15.7 GB RAM, Windows 11):

| Performance Dimension | Organiser Good | Organiser Great | Engineering Goal | Measured (1M) | Measured (10M) | Final Status |
|---|---|---|---|---|---|---|
| **Query Speed: `/orders/count`** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p95 = 7.50 ms` | `p95 = 6.32 ms` | **PASS (Great)** |
| **Query Speed: `/orders/trend`** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p95 = 13.58 ms` | `p95 = 10.74 ms` | **PASS (Great)** |
| **Query Speed: `/orders/by-status`** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p95 = 9.83 ms` | `p95 = 6.09 ms` | **PASS (Great)** |
| **Query Speed: `/products/top`** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p95 = 15.75 ms` | `p95 = 64.44 ms` | **PASS (Great: <200ms)** |
| **Ingestion Freshness Lag** | `< 5 min` | `< 30 s` | `p95 < 5 s` | `p50 = 26.8 ms` | `p50 = 26.6 ms, p95 = 39.3 ms` | **PASS (Great)** |
| **Server-Sent Events (SSE)** | `< 5 s` | `< 500 ms` | `p95 < 100 ms` | `p50 = 40.8 ms` | `p95 = 49.4 ms` | **PASS (Great)** |
| **DB Safety: Triggers & Worker** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `c vs a: -14.7% p50, -30.4% p95 (4.5% write drop)` | `c vs a: +18.5% p50, +16.2% p95 (Good); 2 dash clients: +23.4% p50, +24.1% p95 (Good)` | **PASS (Good: <25%, Great without dashboard load)** |
| **Data Growth (1M -> 10M)** | `< 5x` | `< 2x` | `< 1.5x` | — | **~1x (0.62x to 0.84x)** / **4.09x** | **PASS (Great / Good)** |
| **Correctness (Ground-Truth)** | `exact` | `exact` | `exact` | **480 / 480 passed (100%)** | **480 / 480 at 1M; 10M rerun pending** | **PASS** |

*Note: 10M query latencies on pre-aggregated rollups are within noise of 1M; not a speedup. Growth for top products is 4.09x due to 30-day index heap scans across larger table volume.*  
*Footnote: Raw per-run JSON for 10M queries was not saved to disk; summary values taken from benchmark logs in docs/BENCHMARK.md.*

---

## 5. Security & Authentication Architecture

1. **API Keys**:
   - Tenant requests require `X-Tenant-Key: tenant-key-{tenant_id}`.
   - Administrative requests require `X-Admin-Key: admin-secret-key-12345` (demo key for evaluation; change in production).
2. **Zero-Login Browser Interface**:
   - Web UI served at `http://localhost:8000/ui`.
   - Browser operates with zero credentials. A secure server-side UI proxy (`api/ui_proxy.py`) injects tenant and admin headers server-side, preventing credential leaks to client JavaScript.
3. **Demo Isolation Range**:
   - Demo orders generated via UI simulation use IDs `900,000,000` to `949,999,999`.
   - The UI "Reset Demo" button purges only this explicit range; baseline datasets remain untouched.

---

## 6. Verification & Test Suite

The system is validated by an automated, self-contained test suite of 15 comprehensive suites (`tests/run_all.py`):
1. **Tenant Isolation**: Confirms 0 cross-tenant data leaks.
2. **Idempotency**: Verifies repeated outbox batch application causes 0 delta drift.
3. **Out-of-Order Commits**: Confirms concurrent in-flight transactions are safely deferred via `pg_snapshot_xmin`.
4. **Single-Tenant Fan-Out**: Verifies multi-item orders fold into single net rollup mutations.
5. **Concurrent Multi-Tenant Updates**: Tests 25 tenant databases updating concurrently.
6. **Deficit Round-Robin Fairness**: Proves high-velocity tenants cannot starve quiet neighbors.
7. **Quarantine Handling**: Ensures malformed payloads route to quarantine without killing the daemon.
8. **Crash Recovery**: Verifies `SIGKILL` mid-batch produces 0 duplicate records upon restart.
9. **Per-Tenant Blast Radius Isolation**: Confirms constraint violation on tenant 1 halts only tenant 1.
10. **Rebuild Under Load**: Proves shadow rebuild swaps cleanly without serving interruption.
11. **Golden Parity**: **480 / 480 exact assertions at 1M (10M rerun pending)** comparing rollup API responses against raw independent SQL queries across 10 tenants and 8 temporal boundaries.
12. **B-3 Rate Limiting**: Tests HTTP 429 under 50-client tenant saturation.
13. **B-5 Telemetry**: Asserts rolling latency buffer and tenant health matrix accuracy.
14. **Worker Reconnect**: Verifies automatic socket reconnection with exponential backoff.
15. **Dataset Integrity Tripwire**: Validates exact checksum and row counts of the underlying dataset.
