# Project Progress: Fast Multi-Tenant Aggregation Layer (nextgen)

## Phase Status Summary

| Phase | Description | Status | Gate Validation |
|---|---|---|---|
| **Phase 0** | Recon and Data Setup | **DONE** | Data loaded (1M & 10M), 100% PASS on integrity checks, SCHEMA_NOTES.md complete, config.yaml written, baseline benchmark measured & saved. |
| **Phase 1** | Core Pipeline | **DONE** | Statement-level triggers with transition tables & fan-out, rollup tables with CHECK constraints, safe-xid worker, FastAPI endpoints, rebuild procedure, trigger overhead measured, golden parity 100% (90/90 match), worker_stalled demo passed. |
| **Phase 2** | Trust & Measurement | **DONE** | 100% PASS on unified test suite (`tests/run_all.py`) across both 1M and 10M profiles; official benchmark harness (`bench/bench.py`) measured and saved to `docs/BENCHMARK.md`; fail-closed runbook in `docs/RUNBOOK.md`. |
| **Phase 2.5** | Per-Tenant Database Architecture | **DONE** | Mandatory mentor requirement: 100 separate PostgreSQL databases (`tenant_001` .. `tenant_100`). Multi-source round-robin worker, per-tenant fail-closed, statement triggers inside each DB, 11/11 test suites passing (480/480 golden checks), docs/BENCHMARK.md side-by-side comparison. Git tag `phase2-single-db` preserved. |
| **Part 1** | Event-Driven Pipeline & Optimizations | **DONE** | Lag definition fixed (`now - oldest_pending_ts`), event-driven worker with `LISTEN outbox` & selectors, deficit round-robin fairness, early trigger filter, SSE push endpoint `/tenants/{id}/stream`. |
| **Part 1b** | Closing Checks & Empirical Proving | **DONE** | Git worktree separation (`CyberForge-dashboard` isolated), filter anomaly explained (density + no-ops), 500-op random mutation fuzz passes with 0 mismatches, DB safety final 5 repeats verified ((c vs a) +2.2% p95, Great), log byte-size coincidence explained, SSE cross-tenant auth verified. |
| **Phase 3** | Bonuses | IN PROGRESS | B-3 concurrency throttle (429), B-5 monitor page, B-1/B-2 verification. |
| **Phase 4** | Standout Features | NOT STARTED | One-command scoreboard, try-to-break-it chaos test, live proof page. |
| **Phase 5** | Freeze & Delivery | NOT STARTED | Clean-clone validation, README, backup video, demo rehearsal. |

---

## Phase 2 Execution Details

### Fixes from Phase 1 Feedback
1. **Trigger Overhead Re-Measurement**: Re-measured properly using alternating 3-round OLTP write sequences (baseline vs triggers+worker). Baseline: 363.3 writes/s; Triggers+Worker: 299.6 writes/s (17.5% overhead drop, meeting PRD "Good" <= 20% target).
2. **Extended Golden Parity**: Extended from 90 checks to **480 comprehensive ground-truth checks** covering ALL 4 endpoints (count, hourly trend, daily trend, by-status, top products by sales, top products by units), 10 tenants (including biggest tenant 1 and smallest tenant 99), and 8 range variations (1h, 1d, 7d, 30d, full 2-year span, empty range, current hour, and month boundary).
   - **1M Result:** 480 / 480 passed (100.00% parity, 0 mismatches).
   - **10M Result:** 480 / 480 passed (100.00% parity, 0 mismatches).
3. **Lag Seconds Proof**: Proved via `tests/verify_lag_proof.py`: stopped worker under simulator load, showed `lag_seconds` climbed to 699.87s on the API `/health` endpoint, restarted worker, and showed it returned to 0.00s.
4. **Git Size Cleanliness**: Verified `.gitignore` excludes `data/`, dumps, and temp event logs. `.git` directory size is 3.65 MB.

### Done
1. **Dataset Selection & Profile Architecture**: Configured single-setting profile switching (`PROFILE=1m|10m` via environment variable or `config.yaml`).
2. **10M Database Setup & Rebuild**: Applied statement-level triggers on `shop_10m`, executed `scripts/rebuild.py` into `rollup_10m` (1,538,474 hour rows and 7,332,120 product day rows populated in 207s). Storage reported via `scripts/report_sizes.py`.
3. **Fail-Closed Runbook**: Authored `docs/RUNBOOK.md` detailing operational triage, quarantine inspection, and safe recovery procedures when the fail-closed constraint triggers.
4. **Automated End-to-End Setup**: Authored `scripts/setup_10m.py` enabling 1-command environment deployment from fresh machine.
5. **Unified Test Harness (`tests/run_all.py`)**: Implemented single-command suite running all 9 integrity tests, exiting non-zero on any failure:
   - *Suite 1 (Tenant Isolation):* 403 on missing key, 403 on mismatched tenant key, global cross-tenant partition sum matches raw SQL total.
   - *Suite 2 (Idempotency):* Re-evaluating historical outbox events causes 0 mutations; total rollup counts remain identical.
   - *Suite 3 (Out-of-Order Commit):* T1 starts and holds open, T2 starts and commits; verified worker safe-xid rule (`xid < pg_snapshot_xmin()`) defers T2 until T1 commits, processing both in transaction commit order without skipping.
   - *Suite 4 (Mutation & Fan-Out):* Insert delivered order, cancel (fan-out decrements items), un-cancel (fan-out re-adds items), timestamp shift across day boundary, and order delete. Verified 100% exact parity at each stage.
   - *Suite 5 (Quarantine):* Injected malformed status and invalid negative tenant rows; verified worker routed them to `quarantine` table with diagnosis while continuing healthy operations without stalling.
   - *Suite 6 (Crash Recovery):* Injected high-volume mutations, killed worker abruptly via SIGKILL mid-flight, injected further mutations while worker was offline, restarted worker, and verified rollups caught up to match plain SQL ground truth.
   - *Suite 7 (Fail-Closed CHECK Constraint):* Injected forced negative count outbox delta; verified transaction rolled back on PostgreSQL CHECK constraint, worker halted, and API immediately reported `worker_stalled = true` and `503 Stalled`.
   - *Suite 8 (Rebuild Equality & Snapshot Skip):* Concurrently executed simulator writes while running `rebuild.py` into scratch database; workers processed remaining deltas; proved scratch rebuild and live rollups were 100% identical down to the penny and unit count.
   - *Suite 9 (Golden Ground-Truth Parity):* 480 checks per profile against independent plain SQL.
6. **Benchmark & Trust Harness (`bench/bench.py`)**:
   - Query latency across 10 sample tenants and 4 endpoints (30 timed runs each):
     - 1M p95: 5.82ms (count), 10.06ms (trend), 7.67ms (by-status), 26.34ms (top-products).
     - 10M p95: 15.77ms (count), 30.75ms (trend), 10.66ms (by-status), 82.27ms (top-products). Overall p95: 82.27ms (PRD Good: <= 100ms).
     - Query Scale Factor (10M / 1M): 1.90x (PRD Good: <= 3.0x).
   - Freshness probe latency: p50 206.4ms, p95 219.7ms (PRD Great: <= 0.5s).
   - DB Safety: Triggers + Worker overhead is 17.5% drop (PRD Good: <= 20%).
   - Storage footprint: 1M is 227.6 MB (42.8% of raw); 10M is 1300.4 MB (43.2% of raw) due to high-cardinality product/category keys across 100 tenants.
   - Complete report saved to `docs/BENCHMARK.md`.

---

## Evidence Summary

### 1M Unified Test Suite (`py -3.12 tests/run_all.py --profile 1m`)
```
======================================================================
TEST SUITE SUMMARY (1M):
  1. Tenant Isolation                          :     PASS (0.53s)
  2. Idempotency                               :     PASS (0.13s)
  3. Out-of-Order Commit                       :     PASS (0.89s)
  4. Mutation & Fan-out Handling               :     PASS (1.09s)
  5. Quarantine Handling                       :     PASS (0.22s)
  6. Crash Recovery                            :     PASS (4.79s)
  7. Fail Closed (CHECK Constraint)            :     PASS (2.37s)
  8. Rebuild Equality (Snapshot Skip)          :     PASS (26.31s)
  9. Golden Ground-Truth Parity vs Main-DB     :     PASS (10.70s) [480/480]
======================================================================
SUCCESS: All 9 test suites passed with 100% ground-truth parity!
```

### 10M Unified Test Suite (`py -3.12 tests/run_all.py --profile 10m`)
```
======================================================================
TEST SUITE SUMMARY (10M):
  1. Tenant Isolation                          :     PASS (0.68s)
  2. Idempotency                               :     PASS (0.14s)
  3. Out-of-Order Commit                       :     PASS (0.91s)
  4. Mutation & Fan-out Handling               :     PASS (1.03s)
  5. Quarantine Handling                       :     PASS (0.25s)
  6. Crash Recovery                            :     PASS (1.91s)
  7. Fail Closed (CHECK Constraint)            :     PASS (2.45s)
  8. Rebuild Equality (Snapshot Skip)          :     PASS (247.15s)
  9. Golden Ground-Truth Parity vs Main-DB     :     PASS (70.14s) [480/480]
======================================================================
SUCCESS: All 9 test suites passed with 100% ground-truth parity!
```

---

## Phase 2.5 Execution Details (Per-Tenant Database Architecture)

### Mandatory Requirement & Frozen Decisions
Per mentor mandate, the single shared `shop` database design was replaced by a strict per-tenant database layout:
1. **100 Tenant Databases**: `tenant_001` through `tenant_100` hosted on `cyberforge-main-db` (Postgres 16 on port 5432). Max connections raised to 300.
2. **Central Rollup DB**: Port 5433 (`rollup_1m`). Contains `tenant_registry`, per-tenant `worker_state` rows (`tenant_id PK`), and rollup tables starting with `tenant_id`.
3. **Outbox & Triggers**: Reside inside each tenant DB (statement triggers cannot write across DB boundaries).
4. **Per-Tenant Fail Closed**: If tenant *T* violates check constraints, ONLY tenant *T* stalls (`worker_stalled: true`), while the other 99 tenants continue ingesting seamlessly. Global `/health` reports cluster health and stalled tenant lists.
5. **Multi-Source Worker**: Round-robin scheduler with persistent connections, safe-xid batches, capped per-tenant batch (`500`), and sharding support (`WORKER_ID`/`WORKER_COUNT`).
6. **Per-Tenant Rebuild**: Granular REPEATABLE READ snapshot rebuild per tenant (`POST /admin/rebuild?tenant_id=...`) and parallel full rebuild (`scripts/rebuild.py --profile 1m`). Optimistic row-level locking on `worker_state FOR UPDATE` eliminates rebuild/worker race conditions.

### Test Verification Evidence (`py -3.12 tests/run_all.py --profile 1m`)
```
======================================================================
TEST SUITE SUMMARY (1M):
  1. Tenant Isolation                          :     PASS (1.42s)
  2. Idempotency per-tenant                    :     PASS (0.19s)
  3. Out-of-Order Commit per-tenant            :     PASS (1.33s)
  4. Single-Tenant Mutation & Fan-out          :     PASS (0.87s)
  5. Concurrent Multi-Tenant Updates (25 DBs)  :     PASS (4.37s)
  6. Fairness & Anti-Starvation                :     PASS (1.35s)
  7. Quarantine Handling                       :     PASS (0.24s)
  8. Crash Recovery per-tenant                 :     PASS (6.01s)
  9. Per-Tenant Fail Closed & Stall Isolation  :     PASS (2.92s)
  10. Rebuild Equality under Load per-tenant   :     PASS (8.06s)
  11. Golden Ground-Truth Parity vs Tenant DBs :     PASS (9.55s)
======================================================================
SUCCESS: All 11 test suites passed with 100% ground-truth parity!
GOLDEN CHECK RESULTS: 480/480 passed. Total mismatches: 0
```

### Official Benchmark Comparison (`docs/BENCHMARK.md`)
- **Query Latency p95**: 5.54ms (count), 13.29ms (trend), 6.34ms (by-status), 9.23ms (top products). Flat vs Phase 2 baseline (all `<= 15ms`, PRD Great is `<= 20ms`).
- **Freshness Lag**: p50 120.8ms, p95 208.0ms across 50 random tenant probes (PRD Great is `<= 500ms`).
- **Worker Cycle Time**: 10 tenants = 35.4ms, 50 tenants = 119.0ms, 100 tenants = 216.1ms (~4.6 cluster passes/sec). Idle polling cost is < 2.5% CPU, confirming no NOTIFY machinery is needed.
- **DB Safety**: Alternating 3-round OLTP write overhead: Triggers + Worker = 16.6% drop (PRD Good: <= 20%).
- **Storage Ratio**: 10.12% of raw data (135.7 MB rollup vs 1,340 MB across 100 tenant DBs).

---

## Next Step
- Phase 2.5 is complete, verified, benchmarked, and committed.
- Stopped at Phase 2.5 Gate. Ready for user review and explicit command to "start phase 3".
