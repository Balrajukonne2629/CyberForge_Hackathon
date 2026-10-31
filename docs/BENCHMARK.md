# Benchmark Report: Multi-Tenant Aggregation Layer (Phase 2.5 Multi-Database)

**Generated:** 2026-10-31T16:40:00+00:00  
**Hardware:** 12 vCPUs (AMD Ryzen / WSL2 Linux VM), 15.7 GB RAM, Windows 11 host  
**Database:** PostgreSQL 16.8 (Alpine Docker)  
**Baseline Git Tag:** `phase2-single-db`  
**Dataset Profiles:**  
- Profile `1m`: 100 tenant databases (`tenant_001` .. `tenant_100`), 1,000,000 orders, 1,141,449 items, 3,333,283 customers, 59,951 products.  
- Profile `10m`: 100 tenant databases (`t10m_001` .. `t10m_100`), 10,000,000 orders, 11,416,141 items, 3,333,283 customers, 59,951 products.  

---

## 1. Executive Summary & Organizer Targets

Strict mapping to organizer thresholds and engineering goals without invented tiers:

| Metric / Test | Organizer Good | Organizer Great | Our Goal | Measured Value (Profile 1M) | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Query Speed: `/orders/count`** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p95 = 7.50 ms` (p50 = 4.80 ms, max = 9.86 ms) | **PASS** |
| **Query Speed: `/orders/trend` (hour)** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p95 = 13.58 ms` (p50 = 8.18 ms, max = 16.51 ms) | **PASS** |
| **Query Speed: `/orders/by-status`** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p95 = 9.83 ms` (p50 = 6.89 ms, max = 15.07 ms) | **PASS** |
| **Query Speed: `/products/top` (sales)** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p95 = 20.19 ms` (p50 = 7.47 ms, max = 25.82 ms) | **PASS** |
| **Freshness Lag (50 Sequential Probes)** | `< 5 min` | `< 30 s` | `p95 < 5 s` | `p95 = 228.81 ms` (p50 = 132.30 ms, max = 284.90 ms) | **PASS** |
| **DB Safety: Triggers ON, Worker OFF** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `+1.0% query slowdown` (19.2% write throughput drop) | **PASS** |
| **DB Safety: Triggers ON, Worker ON** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `+16.8% query slowdown` (32.2% write throughput drop) | **PASS (Good)** |
| **DB Safety: 8 Concurrent Dash Clients (d vs c)** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `+86.4% query slowdown` (39.9% write throughput drop) | **FAIL** |
| **DB Safety: Full Stack vs Baseline (d vs a)** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `+117.8% query slowdown` (59.3% write throughput drop) | **FAIL** |
| **Correctness: Golden Dataset Parity** | `exact match` | `exact match` | `exact match` | `480 / 480 passed (0 mismatches)` | **PASS** |
| **Growth 1M to 10M** | `< 5x` | `< 2x` | `< 1.5x` | Awaiting Step 5 Authorization | **PENDING** |

---

## 2. Reconciled DB Safety Breakdown

### 2.1 Latency and Throughput (5 Alternating Repeats)
Measured across 5 alternating rounds (`a -> b -> c -> d -> d -> c -> b -> a ...`), 4.0s sample window per configuration. Medians reported below with measured noise (standard deviation of p95):

| Configuration | OLTP p50 | OLTP p95 | OLTP p99 | Write Throughput | Noise (p95 std) | Overhead vs Baseline (a) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **(a) No Triggers, Worker OFF, Idle** | 1.00 ms | 2.02 ms | 2.85 ms | 255.1 w/s | ± 0.31 ms | Baseline |
| **(b) Triggers ON, Worker OFF** | 1.03 ms | 2.04 ms | 3.36 ms | 206.1 w/s | ± 0.19 ms | +1.0% query slowdown / 19.2% write drop |
| **(c) Triggers ON, Worker ON** | 1.41 ms | 2.36 ms | 3.83 ms | 172.9 w/s | ± 0.23 ms | +16.8% query slowdown / 32.2% write drop |
| **(d) Triggers ON, Worker ON, 8 Dash Clients** | 2.24 ms | 4.40 ms | 8.53 ms | 103.8 w/s | ± 0.71 ms | +117.8% query slowdown / 59.3% write drop |

### 2.2 Slowdown Metrics & Threshold Evaluation
- **(b vs a) Trigger Cost:** p95 latency shifts from 2.02 ms to 2.04 ms (+1.0% slowdown). **PASSES** Organizer Great (<10%) and Our Goal (<5%).
- **(c vs a) Multi-Source Worker Polling Contention:** p95 latency shifts from 2.02 ms to 2.36 ms (+16.8% slowdown). **PASSES** Organizer Good (<25%), fails Great (<10%).
- **(d vs c) Concurrent Dashboard Load Impact:** p95 latency shifts from 2.36 ms to 4.40 ms (+86.4% slowdown). **FAILS** Good (<25%).
- **(d vs a) End-to-End Contention:** p95 latency shifts from 2.02 ms to 4.40 ms (+117.8% slowdown). **FAILS** Good (<25%).

---

## 3. CPU Saturation Proof & Root Cause Analysis

### 3.1 Container & Host Resource Utilization (`docker stats` during (c) and (d))
Captured live during execution of configurations (c) and (d):

| Container / Process | Role | CPU % (c: Worker ON) | CPU % (d: Worker + 8 Dash) | Memory Usage |
| :--- | :--- | :--- | :--- | :--- |
| `cyberforge-main-db` | 100 Tenant DBs (OLTP writes + worker cursor scans) | **80.47%** | **83.12%** | 812 MiB / 15.5 GiB |
| `cyberforge-rollup-db` | Analytical Rollup DB (aggregates & dashboard reads) | **4.30%** | **4.98%** | 196 MiB / 15.5 GiB |
| `uvicorn` (FastAPI) | HTTP API serving dashboard queries | 0.00% | **45.20%** | 88 MiB |
| `dashboard_stress.py` | 8 Concurrent Async Client Threads | 0.00% | **38.40%** | 62 MiB |
| Host Overall (12 vCPUs) | Windows / WSL2 Host Context | **68.2%** | **94.8%** | 12.1 GB / 15.7 GB |

### 3.2 What Drives the Overhead in (d)
1. **Zero Database Query Contention:** Dashboard queries hitting the API exclusively issue `SELECT` queries against `cyberforge-rollup-db` (port 5433). `cyberforge-main-db` (port 5432) receives **0** dashboard queries.
2. **Local Single-Host CPU Core Starvation:** All five components (`cyberforge-main-db`, `cyberforge-rollup-db`, `worker`, `api`, and the benchmark stress load generator) run on the **same 12-core host machine inside a single shared WSL2 virtual machine**. During configuration (d), the 8 client threads and the FastAPI server consume 83.6% CPU between them, starving the Linux CFS scheduler threads servicing PostgreSQL worker connections on `cyberforge-main-db`.
3. **Database Write Contention on `main-db`:** In configuration (c), the worker's persistent round-robin sweeps of 100 databases generate catalog lookups (`pg_snapshot_xmin`) and cursor index scans concurrently with active OLTP `INSERT` and `UPDATE` transactions.

---

## 4. Single-Variable Optimization Fixes (Before / After)

Every optimization was applied and evaluated independently under strict zero-durability-bypass rules (no `UNLOGGED`, `synchronous_commit = on`, `fsync = on`).

### Fix i: Orders UPDATE Trigger Elimination & Delta Folding
- **Problem:** Updating non-metric fields (such as `updated_at`) fired the statement trigger and generated unnecessary outbox records. Updating `total` in the same hour bucket generated redundant `-1` and `+1` count rows.
- **Implementation:** Added transition table filters `o.status IS DISTINCT FROM n.status OR date_trunc('hour', o.created_at) IS DISTINCT FROM date_trunc('hour', n.created_at)` and collapsed same-bucket price updates to a single delta row with `delta_count = 0`.
- **Validation:** Verified via `tests/test_trigger_efficiency.py` (0 outbox rows emitted on no-op updates; exact status delta emitted on transitions; golden parity 480/480 matches).
- **Impact:**

| Metric | Before Fix i | After Fix i | Improvement |
| :--- | :--- | :--- | :--- |
| **No-op UPDATE outbox rows** | 1 row per update | **0 rows** | 100% reduction |
| **OLTP Write Throughput (b)** | 194.2 w/s | **206.1 w/s** | **+6.1%** |
| **OLTP p95 Latency (b)** | 2.11 ms | **2.04 ms** | **-3.3%** |

### Fix ii: Multi-Source Worker Polling Interval Sizing
- **Problem:** Polling 100 tenant databases every 100 ms consumes CPU scanning empty outboxes.
- **Evaluation:** Tested worker idle intervals at 100 ms, 500 ms, and 1000 ms:

| Polling Sleep | Freshness Lag p50 | Freshness Lag p95 | OLTP Query p95 | Evaluation |
| :--- | :--- | :--- | :--- | :--- |
| **100 ms** | 132.3 ms | 228.8 ms | 2.36 ms | Meets p95 < 5s goal, higher DB polling overhead |
| **500 ms** | 412.0 ms | 650.6 ms | 2.18 ms | Meets p95 < 5s goal, reduces DB CPU by ~18% |
| **1000 ms** | 890.5 ms | 1264.2 ms | 2.12 ms | Safely under 5s goal, minimal DB polling impact |

*Decision:* Retained 100 ms default in production configuration to guarantee sub-300ms real-time freshness, while verifying 500ms safely meets the `< 5s` SLA.

### Fix iii: Outbox Index Consolidation
- **Problem:** Tenant outbox tables had duplicate indexing: `outbox_pkey` on `(id)` and a separate composite btree index `outbox_xid_id_idx` on `(xid, id)`. Every outbox row insert updated two btree indexes.
- **Implementation:** Consolidated to a single composite primary key `PRIMARY KEY (xid, id)`.
- **Impact:**

| Metric | Dual Indexes (`id` PK + `xid, id` index) | Single Composite PK `(xid, id)` | Improvement |
| :--- | :--- | :--- | :--- |
| **Outbox Insert Rate** | 886.3 inserts/s | **895.3 inserts/s** | **+1.0%** |
| **Outbox Index Storage** | 32 kB per tenant DB | **16 kB per tenant DB** | **50% storage reduction** |

### Fix iv: CPU Core Pinning (`cpuset`)
- **Problem:** Context switching between `main-db` worker threads and dashboard clients on the shared WSL2 vCPU pool.
- **Implementation:** Evaluated pinning `cyberforge-main-db` to dedicated cores `0-5` and giving `cyberforge-rollup-db`, `api`, and load clients cores `6-11`.
- **Impact:**

| Configuration | Unpinned WSL2 Pool | Pinning `main-db` (0-5) vs Rollup/API (6-11) | Delta |
| :--- | :--- | :--- | :--- |
| **(c) Triggers ON, Worker ON p95** | 2.36 ms | **2.21 ms** | -6.3% |
| **(d) 8 Dash Clients p95** | 4.40 ms | **3.89 ms** | -11.6% |
| **(d) Write Throughput** | 103.8 w/s | **118.2 w/s** | +13.8% |

---

## 5. Storage Footprint (Profile 1M vs Profile 10M)

Reported as plain numbers without status tiers:

| Component | Profile 1M (1M Orders) | Profile 10M (10M Orders) |
| :--- | :--- | :--- |
| **Raw Tenant DBs (100 DBs)** | 1,344.0 MB | 3,826.0 MB |
| **Rollup DB (`rollup_orders_hour`)** | 87.0 MB | 250.0 MB |
| **Rollup DB (`rollup_product_day`)** | 181.0 MB | 1,032.0 MB |
| **Rollup Metadata (`worker_state` + `quarantine`)** | 0.2 MB | 0.1 MB |
| **Total Rollup DB Storage** | **268.2 MB** | **1,282.1 MB** |
| **Rollup Storage Ratio (Rollup / Tenant Storage)** | **19.9%** | **33.5%** |
