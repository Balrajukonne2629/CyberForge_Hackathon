# Frozen Semantics & Business Rules (v1.1 Multi-Database)

## 1. Storage & Multi-Database Topography
1. **Multi-Database Layout**:
   - Each tenant has an independent database (`tenant_001` ... `tenant_100` for 1M; `t10m_001` ... `t10m_100` for 10M).
   - Schema in each tenant database contains: `tenants`, `customers`, `products`, `orders`, `order_items`, and `outbox`.
   - `tenant_id` column is maintained across all tables in every tenant database for schema parity and unified queries.
2. **Central Rollup Store**:
   - Stored in a dedicated database (`rollup_1m` / `rollup_10m`).
   - Every primary key begins with `tenant_id`:
     - `rollup_orders_hour (tenant_id, hour, status)`
     - `rollup_product_day (tenant_id, day, product_id)`
   - Registry and state are centralized:
     - `tenant_registry (tenant_id, database_name, api_key)`
     - `worker_state (tenant_id, cursor_xid, cursor_id, snapshot, lag_seconds, heartbeat, stalled, last_error)`

## 2. Metric Definitions
1. **`order_count`**:
   - Counts **ALL** orders regardless of status (including `canceled` and `unavailable`).
   - Grouped by `(tenant_id, date_trunc('hour', created_at), status)`.
2. **`revenue` / `sales`**:
   - Excludes orders whose status is in the excluded list (`canceled`, `unavailable`).
   - For excluded statuses, the revenue in `rollup_orders_hour` is recorded as `0.00`.
   - For counted orders, `revenue` equals `orders.total` (which matches `sum(qty * price)` on items).
3. **`rollup_product_day` (Top-N and Product Rollup)**:
   - Stores `(tenant_id, date_trunc('day', created_at), product_id, category, units, sales)`.
   - **Only includes items from counted orders** (orders with status NOT IN `('canceled', 'unavailable')`).
   - `units` is the sum of `order_items.qty`.
   - `sales` is the sum of `order_items.qty * order_items.price`.
   - Enforces `CHECK (units >= 0)`.

## 3. Time Bucketing and Range Snapping
1. **Timezone**: All timestamps are treated in UTC.
2. **Order Timestamp**: Always `orders.created_at`.
3. **Snapping Rules**:
   - **Hourly queries** (`/orders/count`, `/orders/trend` with grain=hour, `/orders/by-status`):
     - `from` snaps down to the start of the hour (`YYYY-MM-DD HH:00:00`).
     - `to` snaps up to the start of the next whole hour if not on an exact hour boundary.
     - Intervals are `[from, to)` (from inclusive, to exclusive).
   - **Daily queries** (`/orders/trend` with grain=day, `/products/top`):
     - `from` snaps down to the start of the day (`YYYY-MM-DD 00:00:00`).
     - `to` snaps up to the start of the next whole day if not on an exact day boundary.
     - Intervals are `[from, to)` (from inclusive, to exclusive).
4. **Range Echo**: Every API response returns the exact snapped range in the `range` JSON object:
   ```json
   { "from": "2026-10-01T00:00:00Z", "to": "2026-10-30T00:00:00Z" }
   ```

## 4. Order Lifecycle & Outbox Fan-out
1. **Outbox Local to Tenant DB**:
   - Statement triggers fire on tenant tables and append deltas to that tenant's local `outbox` table.
   - Preserves transactional atomicity without distributed transaction overhead.
2. **Orders INSERT**:
   - Emits signed fact row `(+1 order_count, +revenue if counted, status, hour)`.
   - If parent order is counted, items are inserted separately and emit `(+units, +sales)` into outbox.
3. **Orders UPDATE (Status Change)**:
   - Emits `-1` on old status bucket, `+1` on new status bucket.
   - If timestamp changed, `-1` on old hour, `+1` on new hour.
   - **Fan-out on Counted/Uncounted Flip**:
     - When order moves from counted to excluded (`canceled`, `unavailable`), the trigger emits negative fact rows for each item (`-units, -sales`).
     - When order moves from excluded to counted, trigger emits positive fact rows (`+units, +sales`).
4. **Deletes**:
   - Order items deleted first -> emit negative rows.
   - Orders deleted -> emit negative row.

## 5. Fail-Closed Isolation & Security
1. **Per-Tenant Fail Closed**:
   - If a constraint check fails during batch apply for tenant *T*, only tenant *T* is marked stalled in `worker_state`.
   - Other tenants continue unaffected.
   - API calls for tenant *T* echo `"worker_stalled": true`.
2. **Tenant Authentication & Isolation**:
   - API routes authenticate against `tenant_registry` in `rollup-db`.
   - Request path `/tenants/{tenant_id}/...` must present `X-Tenant-Key` matching `tenant_registry.api_key` for `{tenant_id}` or `ADMIN_KEY`.
   - Mismatched or missing key returns `403 Forbidden`.
