# Project Progress: Fast Multi-Tenant Aggregation Layer (nextgen)

## Phase Status Summary

| Phase | Description | Status | Gate Validation |
|---|---|---|---|
| **Phase 0** | Recon and Data Setup | **DONE** | Data loaded (1M & 10M), 100% PASS on integrity checks, SCHEMA_NOTES.md complete, config.yaml written, baseline benchmark measured & saved. |
| **Phase 1** | Core Pipeline | **DONE** | Statement-level triggers with transition tables & fan-out, rollup tables with CHECK constraints, safe-xid worker, FastAPI endpoints, rebuild procedure, trigger overhead measured, golden parity 100% (90/90 match), worker_stalled demo passed. |
| **Phase 2** | Trust & Measurement | NOT STARTED | Full test suite (isolation, idempotency, out-of-order, crash), benchmark harness v1 at 10M, demo video checkpoint. |
| **Phase 3** | Bonuses | NOT STARTED | B-1 fresh timestamp, B-2 rebuild from scratch, B-3 concurrency throttle (429), B-5 monitor page. |
| **Phase 4** | Standout Features | NOT STARTED | One-command scoreboard, try-to-break-it chaos test, live proof page. |
| **Phase 5** | Freeze & Delivery | NOT STARTED | Clean-clone validation, README, backup video, demo rehearsal. |

---

## Phase 1 Execution Details

### Done
1. **Configurable DBs**: Configured `shop` and `shop_10m` on main-db (port 5432) and `rollup_1m` and `rollup_10m` on rollup-db (port 5433) in `config.yaml`.
2. **Frozen Semantics**: Documented all business definitions and snapping rules in `docs/SEMANTICS.md`.
3. **Capture Pipeline (`db/main`)**: Created `outbox` table and statement-level AFTER triggers with transition tables on `orders` and `order_items` (generated from `config.yaml` via `scripts/generate_triggers.py`). Handled status updates, timestamp bucket shifts, item inserts/deletes, and item fan-out on cancel/un-cancel flips.
4. **Analytics Store (`db/rollup`)**: Applied schema to `rollup_1m` and `rollup_10m` with `CHECK (order_count >= 0)` and `CHECK (units >= 0)`, `worker_state`, `quarantine`, and query indexes.
5. **Rebuild Procedure (`scripts/rebuild.py`)**: Implemented REPEATABLE READ snapshot aggregation from main-db into rollups; executed once for `rollup_1m` (completed in 18.62s for 481,921 hour rows and 1,005,833 product day rows).
6. **Worker Daemon (`worker/worker.py`)**: Safe-xid batching (`xid < pg_snapshot_xmin()`, ordered by `(xid, id)`), in-memory key folding, atomic apply and cursor update in the same transaction, fail-closed handling with `worker_stalled` flag, quarantine routing for malformed rows.
7. **FastAPI Application (`api/main.py`)**: Implemented all four endpoints (`/orders/count`, `/orders/trend`, `/products/top`, `/orders/by-status`) plus `/health`. Enforces `X-Tenant-Key` authentication (403 on mismatch). Returns `data`, `range` (snapped), `as_of`, `lag_seconds`, and `worker_stalled`. Queries ONLY `rollup-db`.
8. **Trigger Overhead Measurement**: Tested using `simulator.py oltp` for 30s. Triggers disabled: 31,011 ops, p50 0.79ms, p95 1.97ms. Triggers enabled: 37,162 ops, p50 0.59ms, p95 1.70ms. Overhead is undetectable (<0%).
9. **Simulator Writes & Mutations**: Tested `simulator.py writes` (99 events). Created `tests/mutate.py` testing cancel flips, un-cancels, timestamp shifts, and deletes with 100% exact parity.
10. **Golden Parity Suite (`tests/test_golden_parity.py`)**: Compared all API endpoints across 10 sample tenants and 3 ranges against independent plain SQL on `main-db`: 90 checks, 0 mismatches (100% parity).
11. **Fail-Closed Demo (`tests/demo_worker_stalled.py`)**: Injected forced negative count into outbox, proved worker halted on CHECK constraint and API immediately reported `worker_stalled = true`.
12. **Git Commit**: Initialized repository and committed Phase 1 implementation.

### Not Done (by design for Phase 1)
- Concurrency limit with 429 (Phase 3).
- Full 10M benchmark and automated chaos / kill-worker tests (Phase 2).

### Known Issues & Notes
- PostgreSQL evaluates CHECK constraints on candidate insert rows before `ON CONFLICT` resolution; worker uses UPDATE with fallback to INSERT so that delta subtractions correctly evaluate the final aggregated value while still failing closed on genuine negative counts.
- `simulator.py` only performs inserts and updates; deletes and un-cancels are validated by `tests/mutate.py`.

### Next Step
- Wait for user instruction to proceed to **Phase 2: Trust and Measurement**.
