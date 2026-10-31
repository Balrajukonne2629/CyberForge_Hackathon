# Benchmark Report: Multi-Tenant Aggregation Layer
**Generated:** 2026-10-31T10:33:44.590268+00:00  
**Hardware:** 12 vCPUs, 15.7 GB RAM, Windows 11 (AMD64)  
**Database:** PostgreSQL 16.8 (Alpine Docker)  
**Git Commit:** `da88e29`  

---

## 1. Executive Summary & PRD Scoreboard

| Metric | Measured | Good Target | Great Target | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Query Latency p95 (10M)** | `82.27 ms` | `<= 100 ms` | `<= 20 ms` | **GOOD** |
| **Query Scale Factor (10M / 1M)** | `1.90x` | `<= 3.0x` | `<= 1.5x (flat)` | **GOOD** |
| **Freshness Lag p95** | `0.220 s (219.7 ms)` | `<= 1.0 s` | `<= 0.5 s` | **GREAT** |
| **OLTP Write Overhead (Triggers+Worker)** | `17.5% drop` | `<= 20%` | `<= 10%` | **GOOD** |
| **OLTP Write Overhead (+Concurrent API)** | `59.7% drop` | `<= 25%` | `<= 15%` | **FAIL** |
| **Rollup Storage Ratio (10M)** | `43.24%` | `<= 15%` | `<= 8%` | **FAIL** |

---

## 2. Query Latency Breakdown (API HTTP localhost)

### 1M Dataset (shop / rollup_1m)
- 10 sample tenants across 30 timed iterations per endpoint (300 calls/endpoint):
| Endpoint | p50 | p95 | max |
| :--- | :--- | :--- | :--- |
| `/orders/count` | 4.03 ms | 5.82 ms | 8.28 ms |
| `/orders/trend (hour)` | 6.29 ms | 10.06 ms | 14.10 ms |
| `/orders/by-status` | 5.20 ms | 7.67 ms | 10.72 ms |
| `/products/top (sales)` | 9.33 ms | 26.34 ms | 52.62 ms |

### 10M Dataset (shop_10m / rollup_10m)
- 10 sample tenants across 30 timed iterations per endpoint:
| Endpoint | p50 | p95 | max | Scale Factor (10M/1M) |
| :--- | :--- | :--- | :--- | :--- |
| `/orders/count` | 9.66 ms | 15.77 ms | 23.66 ms | 2.40x |
| `/orders/trend (hour)` | 17.07 ms | 30.75 ms | 52.35 ms | 2.71x |
| `/orders/by-status` | 7.14 ms | 10.66 ms | 13.65 ms | 1.37x |
| `/products/top (sales)` | 10.48 ms | 82.27 ms | 100.68 ms | 1.12x |

---

## 3. Freshness Probe Latency
50 sequential orders inserted with `created_at = now()`, immediately polling API count until increment:
- **p50:** `206.4 ms`
- **p95:** `219.7 ms`
- **max:** `1009.7 ms`

---

## 4. DB Safety & OLTP Write Overhead
Averaged over 3 alternating rounds (A-B-C-A-B-C-A-B-C):
| Scenario | Throughput | p50 Latency | p95 Latency | p99 Latency | Degradation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **(a) Baseline (Triggers OFF, Worker OFF)** | 363.3 writes/s | 2.49 ms | 4.97 ms | 9.02 ms | Baseline |
| **(b) Triggers ON, Worker ON** | 299.6 writes/s | 3.20 ms | 5.25 ms | 6.33 ms | **17.5% drop** |
| **(c) Triggers ON + Worker + 8 API Clients** | 146.4 writes/s | 6.67 ms | 9.38 ms | 11.25 ms | **59.7% drop** |

---

## 5. Storage Footprint & Compression

| Profile | Raw Main-DB Size | Rollup-DB Size | Rollup Ratio vs Raw |
| :--- | :--- | :--- | :--- |
| **1M Dataset** | 531.8 MB | 227.6 MB | **42.81%** |
| **10M Dataset** | 3007.3 MB | 1300.4 MB | **43.24%** |
