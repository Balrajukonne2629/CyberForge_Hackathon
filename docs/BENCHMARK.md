# CyberForge Benchmark & Empirical Baseline

**Hardware Environment:**
- **Machine / CPU:** Intel64 Family 6 Model 154 Stepping 4, GenuineIntel (12 logical CPUs)
- **Memory (RAM):** 15.7 GB
- **Operating System:** Windows 11 (10.0.26300)
- **Database Engine:** PostgreSQL 16.8 (Alpine) in Docker Desktop (named volumes)
- **Runtime:** Python 3.12.10 x64

---

## 1. Baseline Performance: Direct Main DB (Without Aggregation Layer)

Direct naive queries executed against `main-db` with the baseline indexes from `post_load.sql`:
- `orders (tenant_id, created_at)`
- `order_items (order_id)`
- `order_items (tenant_id, product_id)`

**Methodology:**
- 10 sample tenants spanning the Zipf distribution: Tenants 1, 2, 3, 5, 10, 25, 50, 75, 90, 99.
- 5 warm-up iterations + 30 timed iterations per query per tenant (300 timed runs per query).
- Query range: 30 days (`2026-10-01 00:00:00` to `2026-10-30 00:00:00`).

### 1M Dataset (1,000,000 orders, 1,141,449 items)

| Query | Aggregate p50 | Aggregate p95 | Aggregate Max | Tenant 1 (Largest) p95 |
|---|---|---|---|---|
| **1. Count & Revenue (FR-1)** | 1.18 ms | 6.63 ms | 8.28 ms | 7.38 ms |
| **2a. Hourly Trend (FR-2)** | 1.92 ms | 10.99 ms | 23.14 ms | 13.59 ms |
| **2b. Daily Trend (FR-2)** | 1.29 ms | 9.37 ms | 13.41 ms | 12.53 ms |
| **3. Top 10 Products by Sales (FR-3)** | 11.32 ms | 54.32 ms | 64.70 ms | 57.30 ms |
| **4. Breakdown by Status (FR-4)** | 3.27 ms | 17.69 ms | 21.95 ms | 21.26 ms |

### 10M Dataset (10,000,000 orders, 11,416,141 items)

| Query | Aggregate p50 | Aggregate p95 | Aggregate Max | Tenant 1 (Largest) p95 | 1M -> 10M Slowdown |
|---|---|---|---|---|---|
| **1. Count & Revenue (FR-1)** | 8.65 ms | 122.34 ms | 209.23 ms | 185.04 ms | **18.4x** |
| **2a. Hourly Trend (FR-2)** | 16.94 ms | 200.27 ms | 628.70 ms | 521.89 ms | **18.2x** |
| **2b. Daily Trend (FR-2)** | 19.19 ms | 148.81 ms | 228.42 ms | 215.06 ms | **15.9x** |
| **3. Top 10 Products by Sales (FR-3)** | 93.29 ms | 1,047.10 ms | 1,671.29 ms | 1,443.91 ms | **19.3x** |
| **4. Breakdown by Status (FR-4)** | 9.95 ms | 152.84 ms | 242.83 ms | 212.11 ms | **8.6x** |

### Key Observations & Problem Confirmation
1. **Unbounded Scan Scaling**: In the naive direct model, query latency grows by 15x-20x between 1M and 10M orders.
2. **Failure of Complex Aggregations on Main DB**: The Top-10 Products query at 10M fails the "Good" target (<1s) on the largest tenant (1,443 ms p95, 1,671 ms max).
3. **Noisy Neighbor Impact**: Tenant 1 (representing 19.3% of traffic) requires orders of magnitude more execution time and I/O buffer thrashing than smaller tenants (Tenant 99 executes in ~8ms while Tenant 1 takes ~1,444ms).
4. Direct queries severely endanger the main transactional DB under concurrent dashboard usage, providing the clear motivation for the asynchronous rollup aggregation architecture.
