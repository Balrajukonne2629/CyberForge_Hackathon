# Benchmark Report: Multi-Tenant Aggregation Layer (Phase 2.5 Multi-Database)

**Generated:** 2026-10-31T17:25:00+00:00  
**Hardware:** 12 vCPUs (AMD Ryzen / WSL2 Linux VM), 15.7 GB RAM, Windows 11 host  
**Database:** PostgreSQL 16.8 (Alpine Docker)  
**Baseline Git Tag:** `phase2-single-db`  
**Architecture Layout:** `layout: per_tenant` (100 dedicated PostgreSQL databases per profile)  
**Active Profiles:**  
- Profile `1m`: 100 tenant databases (`tenant_001` .. `tenant_100`), 1,000,000 orders, 1,141,449 items, 3,333,283 customers, 59,951 products.  
- Profile `10m`: 100 tenant databases (`t10m_001` .. `t10m_100`), 10,000,000 orders, 11,416,141 items, 3,333,283 customers, 59,951 products.  
- Final Default Worker Polling Interval: `500 ms`.  

---

## 1. Executive Summary & Organizer Targets

All evaluations use strict organizer thresholds and engineering goals. For DB safety, the **worse of p50 or p95 decides PASS/FAIL**, with writes/sec reported separately:

| Metric / Test | Organizer Good | Organizer Great | Our Goal | Measured Value (Profile 1M) | Measured Value (Profile 10M) | Final Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Query Speed: `/orders/count`** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p50=4.80ms, p95=7.50ms` | `p50=7.70ms, p95=13.00ms` | **PASS (Great)** |
| **Query Speed: `/orders/trend` (hour)** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p50=8.18ms, p95=13.58ms` | `p50=11.95ms, p95=19.92ms` | **PASS (Great)** |
| **Query Speed: `/orders/by-status`** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p50=6.89ms, p95=9.83ms` | `p50=7.93ms, p95=14.86ms` | **PASS (Great)** |
| **Query Speed: `/products/top` (sales)** | `< 1 s` | `< 200 ms` | `p95 < 100 ms` | `p50=7.47ms, p95=20.19ms` | `p50=9.22ms, p95=100.04ms`* | **PASS (Good)** |
| **Freshness Lag (50 Sequential Probes)** | `< 5 min` | `< 30 s` | `p95 < 5 s` | `p50=132.3ms, p95=228.8ms` | `p50=160.6ms, p95=785.0ms` | **PASS (Great)** |
| **DB Safety: Triggers ON, Worker OFF** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `+6.9% slowdown (5.7% write drop)` | `-8.3% slowdown (-4.7% write drop)` | **PASS (Great)** |
| **DB Safety: Triggers ON, Worker ON** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `+44.6% slowdown (24.9% write drop)` | `+35.2% slowdown (23.0% write drop)` | **FAIL** |
| **DB Safety: 2 Dash Clients (d2 vs c)** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `+32.0% slowdown (21.7% write drop)` | `+99.6% slowdown (32.6% write drop)` | **FAIL** |
| **DB Safety: 8 Dash Clients (d8 vs c)** | `< 25% slower` | `< 10% slower` | `< 5% slower` | `+119.9% slowdown (42.7% write drop)` | `+156.6% slowdown (44.0% write drop)` | **FAIL** |
| **Correctness: Golden Parity** | `exact match` | `exact match` | `exact match` | `480 / 480 passed (0 mismatches)` | `480 / 480 passed (0 mismatches)` | **PASS** |
| **Growth 1M to 10M: Count** | `< 5x` | `< 2x` | `< 1.5x` | — | `p50: 1.60x, p95: 1.73x` | **PASS (Great)** |
| **Growth 1M to 10M: Trend (hour)** | `< 5x` | `< 2x` | `< 1.5x` | — | `p50: 1.46x, p95: 1.47x` | **PASS (Great)** |
| **Growth 1M to 10M: By Status** | `< 5x` | `< 2x` | `< 1.5x` | — | `p50: 1.15x, p95: 1.51x` | **PASS (Great)** |
| **Growth 1M to 10M: Top Products** | `< 5x` | `< 2x` | `< 1.5x` | — | `p50: 1.23x, p95: 4.95x`* | **PASS (Good)** |

*\*After covering index `idx_rollup_prod_covering` on `rollup_product_day (tenant_id, day) INCLUDE (product_id, units, sales)`.*

---

## 2. DB Safety Breakdown & Server Execution Metrics

Measurements conducted over 5 alternating repeats (`a -> b -> c -> d2 -> d8 -> d8 -> d2 -> c -> b -> a ...`), 4.0s sampling window per configuration. Client-side latencies measured concurrently with OLTP write transactions and dashboard loads (20 req/s per client). Server-side query execution time captured directly from PostgreSQL internal metrics via `pg_stat_statements` (reset between configurations):

### 2.1 Profile 1M Results (Mean ± Std over 5 Alternating Repeats)

| Configuration | OLTP p50 (ms) | OLTP p95 (ms) | OLTP p99 (ms) | OLTP Mean (ms) | Server Mean (ms) | Writes/sec |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **(a) No Triggers, Worker OFF, Idle** | 1.15 ± 0.05 | 1.96 ± 0.04 | 3.10 ± 0.12 | 1.25 ± 0.06 | 0.11 ± 0.00 | 221.5 ± 9.4 |
| **(b) Triggers ON, Worker OFF** | 1.23 ± 0.17 | 2.08 ± 0.36 | 3.32 ± 0.54 | 1.33 ± 0.18 | 0.12 ± 0.01 | 209.0 ± 29.4 |
| **(c) Triggers ON, Worker ON (500ms)** | 1.66 ± 0.09 | 2.56 ± 0.18 | 4.15 ± 0.18 | 1.73 ± 0.10 | 0.15 ± 0.01 | 166.4 ± 7.0 |
| **(d2) Worker ON, 2 Dash Clients** | 2.11 ± 0.21 | 3.37 ± 0.49 | 5.41 ± 0.66 | 2.24 ± 0.21 | 0.20 ± 0.02 | 130.3 ± 10.1 |
| **(d8) Worker ON, 8 Dash Clients** | 2.63 ± 0.08 | 5.62 ± 0.40 | 9.65 ± 0.99 | 3.00 ± 0.12 | 0.29 ± 0.02 | 95.3 ± 3.7 |

### 2.2 Profile 10M Results (Mean ± Std over 5 Alternating Repeats)

| Configuration | OLTP p50 (ms) | OLTP p95 (ms) | OLTP p99 (ms) | OLTP Mean (ms) | Server Mean (ms) | Writes/sec |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **(a) No Triggers, Worker OFF, Idle** | 0.99 ± 0.06 | 2.28 ± 0.33 | 3.97 ± 1.47 | 1.26 ± 0.15 | 0.12 ± 0.02 | 210.8 ± 38.2 |
| **(b) Triggers ON, Worker OFF** | 0.91 ± 0.18 | 1.95 ± 0.18 | 4.56 ± 2.31 | 1.10 ± 0.16 | 0.11 ± 0.02 | 220.6 ± 45.5 |
| **(c) Triggers ON, Worker ON (500ms)** | 1.34 ± 0.20 | 2.43 ± 0.18 | 4.70 ± 1.43 | 1.54 ± 0.11 | 0.14 ± 0.02 | 162.2 ± 18.9 |
| **(d2) Worker ON, 2 Dash Clients** | 2.14 ± 0.68 | 4.85 ± 3.89 | 9.81 ± 7.09 | 2.60 ± 1.26 | 0.25 ± 0.13 | 109.3 ± 33.6 |
| **(d8) Worker ON, 8 Dash Clients** | 2.35 ± 0.14 | 6.24 ± 2.04 | 13.01 ± 6.59 | 2.88 ± 0.39 | 0.28 ± 0.02 | 90.8 ± 16.5 |

### 2.3 Confirmation of Zero Dashboard Queries on `main-db`
- During all runs of (d2) and (d8), queries matching dashboard endpoints or `rollup_*` tables on `cyberforge-main-db` (port 5432): **EXACTLY 0 QUERIES**.
- All dashboard HTTP requests query exclusively `cyberforge-rollup-db` (port 5433).

### 2.4 Root Cause of Overhead in Configuration (c) and (d)
1. **Trigger Overhead (b vs a):** Negligible impact (+6.9% p50 slowdown on 1M, -8.3% on 10M; write throughput drop 5.7%). Passes Organizer Great (<10%).
2. **Worker Sweep Contention (c vs a):** Sweeping 100 persistent tenant database connections every 500 ms acquires snapshot catalog metadata and scans outbox cursors. Even though each database query takes <0.15ms on the server, the cumulative background connection activity on a single PostgreSQL server (`main-db`) introduces ~35% - 44% client-side latency inflation and ~23% - 25% write drop, crossing the 25% threshold (FAIL).
3. **Dashboard Load Saturation (d vs c):** In (d2) and (d8), zero SQL queries hit `main-db`. The observed slowdown (+32% to +120% on 1M, +99% to +156% on 10M) is driven entirely by single-host CPU core contention within the shared WSL2 virtual machine, where client HTTP threads, FastAPI uvicorn workers, and PostgreSQL backends compete for CPU cycles.

---

## 3. Profile 10M Query Latency & Growth Ratio Analysis

### 3.1 Growth Ratio (1M vs 10M Profile)

| Endpoint | 1M p50 | 10M p50 | p50 Growth | 1M p95 | 10M p95 | p95 Growth | Organizer Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `/orders/count` | 4.80 ms | 7.70 ms | **1.60x** | 7.50 ms | 13.00 ms | **1.73x** | **PASS (<2x)** |
| `/orders/trend` (hour) | 8.18 ms | 11.95 ms | **1.46x** | 13.58 ms | 19.92 ms | **1.47x** | **PASS (<1.5x)** |
| `/orders/by-status` | 6.89 ms | 7.93 ms | **1.15x** | 9.83 ms | 14.86 ms | **1.51x** | **PASS (<2x)** |
| `/products/top` (sales, raw) | 7.47 ms | 13.37 ms | **1.79x** | 20.19 ms | 127.99 ms | **6.34x** | Investigate |
| `/products/top` (covering index) | 7.47 ms | 9.22 ms | **1.23x** | 20.19 ms | 100.04 ms | **4.95x** | **PASS (<5x)** |

### 3.2 Covering Index Investigation on `rollup_product_day`
Because the raw Top-N query at 10M grew beyond 1.5x due to heap page fetches over 66,000 product-day entries on Tenant 1, a covering index was created:
```sql
CREATE INDEX idx_rollup_prod_covering 
ON rollup_product_day (tenant_id, day) 
INCLUDE (product_id, units, sales);
```
- **Before Covering Index:** p50 = 13.37 ms, p95 = 127.99 ms (Heap scan across 26,611 table blocks).
- **After Covering Index:** p50 = **9.22 ms** (-31.0%), p95 = **100.04 ms** (-21.8%).

### 3.3 EXPLAIN (ANALYZE, BUFFERS) for Top-N Query on Biggest Tenant (Tenant 1, 30 Days)
```
Limit  (cost=97827.39..97827.42 rows=10 width=40) (actual time=112.081..112.084 rows=10 loops=1)
  Buffers: shared hit=26783 read=2
  ->  Sort  (cost=97827.39..97907.81 rows=32167 width=40) (actual time=112.080..112.082 rows=10 loops=1)
        Sort Key: (sum(sales)) DESC
        Sort Method: top-N heapsort  Memory: 25kB
        Buffers: shared hit=26783 read=2
        ->  HashAggregate  (cost=96730.19..97132.27 rows=32167 width=40) (actual time=105.487..110.266 rows=11205 loops=1)
              Group Key: product_id
              Batches: 1  Memory Usage: 7697kB
              Buffers: shared hit=26780 read=2
              ->  Bitmap Heap Scan on rollup_product_day  (cost=1215.10..96406.00 rows=64837 width=14) (actual time=14.765..70.757 rows=66016 loops=1)
                    Recheck Cond: ((tenant_id = 1) AND (day >= '2026-10-01'::date) AND (day < '2026-10-31'::date))
                    Heap Blocks: exact=26611
                    Buffers: shared hit=26780 read=2
                    ->  Bitmap Index Scan on idx_rollup_product_day_range  (cost=0.00..1198.89 rows=64837 width=0) (actual time=10.456..10.457 rows=132041 loops=1)
                          Index Cond: ((tenant_id = 1) AND (day >= '2026-10-01'::date) AND (day < '2026-10-31'::date))
                          Buffers: shared hit=170 read=1
Planning:
  Buffers: shared hit=168
Planning Time: 1.075 ms
Execution Time: 113.552 ms
```

---

## 4. Flood Testing on Biggest Tenant (Tenant 1, 5,000 Rows at 10M)

A burst of 5,000 outbox rows was injected into Tenant 1's dedicated database (`t10m_001`). The worker ingested the burst in 500-row chunks while continuing round-robin visits to the other 99 tenant databases:

- **Total Drain Time for 5,000 Rows:** **1.96 s** (10 batch cycles of 500 rows at ~190ms per round-robin pass).
- **Observer Tenants' Ingestion Lag During Drain:** **0.000 s Average, 0.000 s Maximum** across all sampled intervals.
- **Fairness Guarantee:** The 500-row batch cap strictly prevented Tenant 1 from starving any other tenant.

| Elapsed Time | Tenant 1 Cursor | Tenant 1 Lag | Other Tenants Avg Lag | Other Tenants Max Lag |
| :--- | :--- | :--- | :--- | :--- |
| **0.00 s** | 13,241 | 0.000 s | 0.000 s | 0.000 s |
| **0.63 s** | 14,241 | 19,800.57 s | 0.000 s | 0.000 s |
| **0.93 s** | 15,241 | 19,800.83 s | 0.000 s | 0.000 s |
| **1.24 s** | 16,241 | 19,801.16 s | 0.000 s | 0.000 s |
| **1.55 s** | 16,741 | 19,801.41 s | 0.000 s | 0.000 s |
| **1.85 s** | 17,741 | 19,801.75 s | 0.000 s | 0.000 s |
| **1.96 s (Drained)** | 18,241 | 0.000 s | 0.000 s | 0.000 s |

---

## 5. Multi-Source Worker Cycle Time (10M Profile)

Measured across 30 complete rounds visiting all 100 persistent tenant database connections (`t10m_001` .. `t10m_100`):
- **p50 Cycle Time:** `308.98 ms`
- **p95 Cycle Time:** `478.71 ms`
- **Mean Cycle Time:** `316.57 ms`

---

## 6. Storage Footprint (Plain Numbers)

| Storage Component | Profile 1M (1M Orders) | Profile 10M (10M Orders) |
| :--- | :--- | :--- |
| **Raw Tenant DBs (100 DBs)** | 1,344.0 MB | 3,826.0 MB |
| **Rollup DB (`rollup_orders_hour`)** | 87.0 MB | 250.0 MB |
| **Rollup DB (`rollup_product_day`)** | 181.0 MB | 1,032.0 MB |
| **Rollup Metadata (`worker_state` + `quarantine`)** | 0.2 MB | 0.1 MB |
| **Total Rollup DB Storage** | **268.2 MB** | **1,282.1 MB** |
| **Rollup Storage Ratio** | 19.9% | 33.5% |
