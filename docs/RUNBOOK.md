# Operations Runbook: Multi-Tenant Aggregation Layer

## 1. Fail-Closed Architecture & Alerting

The rollup aggregation layer operates under a **fail-closed** contract:
- Pre-aggregated tables enforce database-level check constraints:
  - `rollup_orders_hour (CHECK order_count >= 0)`
  - `rollup_product_day (CHECK units >= 0)`
- When an outbox delta would result in a negative count or units:
  1. The transaction automatically rolls back.
  2. The worker sets `worker_stalled = true` and records the exact constraint error message in `worker_state.stalled_reason`.
  3. The worker halts processing to prevent state corruption.
  4. The API echoes `"worker_stalled": true` on all dashboard queries and `/health` reports `"status": "stalled"`.

---

## 2. Operator Diagnostic Steps

When alerted to `worker_stalled: true`:

### Step 1: Inspect Worker State & Error Reason
Execute against the rollup database (`rollup_1m` or `rollup_10m`):
```sql
SELECT cursor_xid, cursor_id, lag_seconds, worker_stalled, stalled_reason, heartbeat
FROM worker_state
WHERE id = 1;
```

### Step 2: Locate Offending Row in Main DB Outbox
The cursor points to the last successful row. The offending row is the next unconsumed row:
```sql
SELECT id, xid, kind, tenant_id, bucket_ts, status, product_id, delta_count, delta_amount, created_at
FROM outbox
WHERE (xid > (SELECT cursor_xid FROM worker_state WHERE id = 1))
   OR (xid = (SELECT cursor_xid FROM worker_state WHERE id = 1) AND id > (SELECT cursor_id FROM worker_state WHERE id = 1))
ORDER BY xid ASC, id ASC
LIMIT 5;
```

---

## 3. Operator Remediation Procedures

### Procedure A: Route Offending Row to Quarantine
If the outbox row is malformed or represents an invalid external decrement:
1. Copy the payload to the `quarantine` table on rollup-db:
   ```sql
   INSERT INTO quarantine (outbox_id, payload, reason)
   VALUES (
       <offending_id>,
       '<row_json>',
       'Operator quarantined: caused negative count constraint violation'
   );
   ```
2. Delete the offending row from `outbox` on main-db:
   ```sql
   DELETE FROM outbox WHERE id = <offending_id>;
   ```
3. Clear the stalled flag in `worker_state`:
   ```sql
   UPDATE worker_state 
   SET worker_stalled = false, stalled_reason = NULL, heartbeat = clock_timestamp()
   WHERE id = 1;
   ```
4. Restart the worker process:
   ```powershell
   py -3.12 worker/worker.py <main_db_url> <rollup_db_url>
   ```

### Procedure B: Full Rebuild from Main DB
If rollups are suspected to have diverged systemically:
1. Execute the zero-downtime rebuild procedure:
   ```powershell
   py -3.12 scripts/rebuild.py <main_db_url> <rollup_db_url>
   ```
2. The rebuild captures a new `REPEATABLE READ` snapshot, re-aggregates fresh rollups, updates `worker_state`, and resets `worker_stalled = false`.
3. Worker automatically skips any outbox rows visible in that snapshot (`pg_visible_in_snapshot`).
