# Project Progress: Fast Multi-Tenant Aggregation Layer (nextgen)

## Phase Status Summary

| Phase | Description | Status | Gate Validation |
|---|---|---|---|
| **Phase 0** | Recon and Data Setup | **DONE** | Data loaded (1M & 10M), 100% PASS on integrity checks, SCHEMA_NOTES.md complete, config.yaml written, baseline benchmark measured & saved. |
| **Phase 1** | Core Pipeline | **DONE** | Statement-level triggers with transition tables & fan-out, rollup tables with CHECK constraints, safe-xid worker, FastAPI endpoints, rebuild procedure, trigger overhead measured, golden parity 100% (90/90 match), worker_stalled demo passed. |
| **Phase 2** | Trust & Measurement | **DONE** | 100% PASS on unified test suite (`tests/run_all.py`) across both 1M and 10M profiles (isolation, idempotency, out-of-order, mutations, quarantine, crash recovery, fail closed, rebuild equality under load, 480/480 golden parity checks); official benchmark harness (`bench/bench.py`) measured and saved to `docs/BENCHMARK.md`; fail-closed runbook in `docs/RUNBOOK.md`; single setup script in `scripts/setup_10m.py`. |
| **Phase 3** | Bonuses | NOT STARTED | B-1 fresh timestamp, B-2 rebuild from scratch, B-3 concurrency throttle (429), B-5 monitor page. |
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

## Next Step
- Phase 2 is complete and verified on both profiles.
- Stopped at Phase 2 Gate. Ready for user review and prompt to begin **Phase 3: Bonuses**.
