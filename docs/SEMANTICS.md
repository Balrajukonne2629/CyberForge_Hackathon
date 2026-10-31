# Frozen Semantics & Business Rules

## 1. Metric Definitions
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

## 2. Time Bucketing and Range Snapping
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

## 3. Order Lifecycle & Fan-out Transitions
1. **Orders INSERT**:
   - Emits signed fact row `(+1 order_count, +revenue if counted, status, hour)`.
   - If parent order is counted, items are inserted separately and emit `(+units, +sales)` into outbox.
2. **Orders UPDATE (Status Change)**:
   - Always emits `-1` on old status bucket, `+1` on new status bucket.
   - If timestamp changed, `-1` on old hour, `+1` on new hour.
   - **Fan-out on Counted/Uncounted Flip**:
     - When order moves from counted (`delivered`, `shipped`, `invoiced`, `processing`, `created`, `approved`) to excluded (`canceled`, `unavailable`), the trigger emits **negative fact rows** for each of the order's items (`-units, -sales`) to subtract them from `rollup_product_day`.
     - When an order moves from excluded to counted, the trigger emits **positive fact rows** (`+units, +sales`) for each item.
3. **Deletes**:
   - Order items deleted first -> emit negative rows.
   - Orders deleted -> emit negative row.
   - If parent order was already cancelled, items were never counted in rollups so no item subtraction is needed.

## 4. Tenant Isolation & Security
- Every rollup table primary key starts with `tenant_id`.
- Every query to the rollup database filters on `tenant_id`.
- The API requires header `X-Tenant-Key`. If missing, invalid, or belonging to a tenant other than the path parameter `{tenant_id}`, the API returns `403 Forbidden`.
- The dashboard never reads from `main-db`.
