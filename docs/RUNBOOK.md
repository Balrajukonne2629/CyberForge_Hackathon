# Operations Runbook: Multi-Database Multi-Tenant Aggregation Layer

## 1. Per-Tenant Fail-Closed Architecture

The system enforces per-tenant blast radius containment:
- Rollup tables enforce database-level integrity constraints:
  - `rollup_orders_hour (CHECK order_count >= 0)`
  - `rollup_product_day (CHECK units >= 0)`
- When an outbox delta for tenant *T* violates a constraint:
  1. The transaction for tenant *T* on `rollup-db` is rolled back.
  2. The worker marks `worker_state.stalled = true` and logs `last_error` specifically for `tenant_id = T`.
  3. Ingestion halts **only for tenant *T***. Ingestion for all other 99 tenants continues without interruption.
  4. API queries for tenant *T* echo `"worker_stalled": true`.
  5. The global `/health` endpoint indicates `stalled` with the count and list of stalled tenant IDs.

---

## 2. Operator Diagnostic Steps

When alerted that a tenant is stalled:

### Step 1: Query Stalled Tenants in Rollup DB
Execute against `rollup_1m` or `rollup_10m`:
```sql
SELECT tenant_id, cursor_xid, cursor_id, lag_seconds, stalled, last_error, heartbeat
FROM worker_state
WHERE stalled = true;
```

### Step 2: Locate Offending Row in Tenant DB Outbox
Connect directly to that tenant's database (e.g., `tenant_005`):
```sql
SELECT id, xid, kind, tenant_id, bucket_ts, status, product_id, delta_count, delta_amount, created_at
FROM outbox
WHERE (xid > (SELECT cursor_xid FROM dblink('...', 'SELECT cursor_xid FROM worker_state WHERE tenant_id = 5') AS t(cursor_xid xid8)))
ORDER BY xid ASC, id ASC
LIMIT 5;
```
Or use the cursor values reported from Step 1:
```sql
SELECT id, xid, kind, tenant_id, bucket_ts, status, product_id, delta_count, delta_amount, created_at
FROM outbox
WHERE (xid > '<cursor_xid>'::xid8) OR (xid = '<cursor_xid>'::xid8 AND id > <cursor_id>)
ORDER BY xid ASC, id ASC
LIMIT 5;
```

---

## 3. Operator Remediation Procedures

### Procedure A: Quarantine the Corrupted Outbox Row
1. Copy the offending payload into `quarantine` on `rollup-db`:
   ```sql
   INSERT INTO quarantine (tenant_id, outbox_id, payload, reason)
   VALUES (
       <tenant_id>,
       <offending_id>,
       '<row_json>',
       'Operator quarantined: negative count constraint violation'
   );
   ```
2. Delete the offending row from `outbox` in the tenant's database:
   ```sql
   DELETE FROM outbox WHERE id = <offending_id>;
   ```
3. Clear the stalled flag for that tenant in `rollup-db`:
   ```sql
   UPDATE worker_state 
   SET stalled = false, last_error = NULL, heartbeat = clock_timestamp()
   WHERE tenant_id = <tenant_id>;
   ```
4. The worker automatically resumes ingestion for that tenant on its next cycle.

### Procedure B: Targeted Single-Tenant Rebuild (Recommended)
If rollups for a single tenant diverged or stalled due to outbox inconsistencies, execute a surgical single-tenant rebuild:

1. **Check Stall via Operational Telemetry**:
   ```bash
   curl -s -H "X-Admin-Key: admin-secret-key-12345" http://localhost:8000/admin/monitor | jq '.tenants[] | select(.stalled == true)'
   ```
2. **Trigger Targeted Rebuild via Admin API**:
   ```bash
   curl -X POST "http://localhost:8000/admin/rebuild?tenant_id=<tenant_id>" \
        -H "X-Admin-Key: admin-secret-key-12345"
   ```
   *Expected Response*:
   ```json
   {
     "status": "success",
     "scope": "tenant_<tenant_id>",
     "result": { "tenant_id": <tenant_id>, "status": "completed" }
   }
   ```
3. **Alternative CLI Rebuild**:
   ```powershell
   py -3.12 scripts/rebuild.py --profile 1m --tenant-id <tenant_id>
   ```
4. **How the Targeted Rebuild Works**:
   - Captures a `REPEATABLE READ` snapshot on the tenant's individual database.
   - Atomically deletes existing rollup records in `rollup_orders_hour` and `rollup_product_day` *only* for `tenant_id = <tenant_id>`.
   - Re-aggregates raw `orders` and `order_items` tables into clean rollups.
   - Updates `worker_state` for that tenant: sets `stalled = false`, clears `last_error`, advances `cursor_xid` to the snapshot upper bound, and resets `lag_seconds = 0.0`.
   - Executes `VACUUM (ANALYZE)` on the affected tables.
5. **Verify Recovery**:
   ```bash
   curl -s -H "X-Admin-Key: admin-secret-key-12345" http://localhost:8000/admin/monitor | jq '.tenants[] | select(.tenant_id == <tenant_id>)'
   ```
   Ensure `"stalled": false` and `"lag_seconds": 0.0`.
