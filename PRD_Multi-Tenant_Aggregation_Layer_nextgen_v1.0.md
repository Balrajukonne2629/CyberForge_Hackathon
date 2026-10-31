# PRD: Multi-Tenant Aggregation Layer (Team nextgen)

**Event:** Mitigata CyberForge, finals, 24 hours (11:00 to 11:00)
**Problem statement:** #1 Fast Multi-Tenant Data Aggregation
**Team:** Balu, Vignesh Thati
**Version:** 1.0, 31 Oct 2026
**Status:** Final. Replaces v0.1 and v0.2. The organiser PRD (28 Oct 2026, Aroop Ghosh) is the authority for requirements and targets. Items marked **[VERIFY]** must be checked against `schema.sql` and the CSV headers before any SQL is written (section 14). Numbers marked *estimate* are untested.

---

## 1. Summary

We build an aggregation layer that sits between tenant dashboards and the main database. The main application keeps writing to the main database as it does today. Our layer captures those changes, keeps pre-computed numbers (rollups) in a separate analytics store, and answers dashboard queries from the rollups. Dashboards never run heavy queries on the main database.

Domain is shopping / e-commerce, exactly as the organiser PRD describes. We do not change domain.

We win on **evidence**: a one-command benchmark that shows every organiser target, a correctness test that tries to break the numbers, and a live demo that shows the main database staying healthy under dashboard load.

## 2. Source requirements (from the organiser PRD)

**Must have**

| ID | Requirement |
|---|---|
| FR-1 | Counts and totals for a time range |
| FR-2 | Trend over time (per hour or per day) |
| FR-3 | Top-N list (for example top 10 products by sales) |
| FR-4 | Breakdown by a category (for example orders by status) |
| FR-5 | Tenant filter on every query; a tenant never sees another tenant's data |
| FR-6 | New data and updates (for example placed to cancelled) show up in results |
| FR-7 | Simple REST API with a short README of the endpoints |

**Bonus (pick any)**

| ID | Requirement |
|---|---|
| B-1 | Response shows how fresh the data is |
| B-2 | Rebuild all numbers from scratch |
| B-3 | Limit heavy queries per tenant |
| B-4 | Add a new metric by editing a config file, not code |
| B-5 | Simple monitoring page (query time, data lag) |

**Performance targets** (10 million records, 100 tenants, normal laptop)

| Measure | Good | Great | Our target |
|---|---|---|---|
| Query speed (each dashboard query, 10M) | under 1 s | under 200 ms | p95 under 100 ms |
| Freshness (insert to visible) | under 5 min | under 30 s | p95 under 5 s |
| Database safety (normal app queries, with vs without dashboard load) | under 25% slower | under 10% slower | under 5% slower |
| Correctness vs direct database query | most match | all match | all match, exact |
| Growth (1M vs 10M query time) | under 5x slower | under 2x slower | under 1.5x slower |

Our targets are plans, not results. Reports always show the organiser thresholds next to ours. We claim nothing until the benchmark has run.

Not required: polished UI, login, payments, cloud deployment, machine learning. Judges must be able to start the system from the README; Docker Compose is a plus.

## 3. Goals and non-goals

**Goals**
1. Meet every "Great" target and prove it with numbers anyone can reproduce.
2. Complete FR-1 to FR-7 and bonuses B-1, B-2, B-3, B-5. B-4 if time allows.
3. Be explainable: both team members can describe every component.
4. Add standout features only after the core and the tests are done (phase 4).

**Non-goals**
- A polished dashboard UI. A simple page is enough.
- Authentication beyond per-tenant API keys.
- Cloud deployment or multi-node clustering.
- Machine learning, language-model features, or any other domain. Sarvam is not used unless a mentor says it is scored.
- Generating our own data. The dataset is supplied (section 4).
- Claiming scale we did not measure. All numbers are labelled with the hardware they came from.

## 4. Dataset (supplied, do not modify)

| Item | Value |
|---|---|
| Domain | E-commerce, synthetic, patterns from the public Olist dataset |
| Main set | 10,000,000 orders, 100 tenants |
| Small set | 1,000,000 orders, same 100 tenants, subset of the main set |
| Order items | 11,416,141 (main) |
| Customers | about 3.33 million |
| Products | about 59,951 |
| Time range | 31 Oct 2024 to 30 Oct 2026 |
| Format | `.csv.gz` files plus `schema.sql`, `load.sh`, `post_load.sql` |
| Reported load time | 108 s (10M), 17 s (1M). Reported by the generator process, not verified by us |
| Tools shipped | `validate_data.py`, `make_slice.py`, `simulator.py`, `generate_data.py` |

Rules:
- The data is synthetic. README and pitch say so. Never call it real transactions.
- We never modify the original rows. We only **add** objects to the main DB: an outbox table, triggers, functions. The cost of those is measured in the DB safety test.
- The data ends on 30 Oct 2026, so "last 7 days" and "last 30 days" queries have real data on the day of the finals.
- We rerun `validate_data.py` ourselves and save the output. We do not trust the reported results unchecked.

**Simulator (`simulator.py`)**
- *Writes mode:* creates new orders, moves them placed to shipped to delivered, cancels some, and logs commit times. We use it for the freshness probe and the update tests.
- *OLTP mode:* times normal application queries on the main DB. We use it for the DB safety test.

## 5. Decisions

Most open decisions from v0.1 are now settled by the dataset or by the organiser PRD. What is left is marked.

| ID | Decision | Resolution |
|---|---|---|
| DD-1 | Main database engine | PostgreSQL 16 (dataset). Triggers, no change streams |
| DD-2 | Tenant model | One shared database with a `tenant_id` column. **[VERIFY]** it is present on orders, order_items, products and customers. If order_items has none, derive it from the order |
| DD-3 | May we add triggers and tables to the main DB? | Assumed yes. Ask a mentor. If no, polling fallback in section 6.4 |
| DD-4 | Time range semantics | Snapped to whole hours (count, trend, by-status) or whole days (top-N). Response echoes the snapped range. See section 8 |
| DD-5 | Timezone | UTC, `from` inclusive, `to` exclusive |
| DD-6 | Definition of "sales" | Excludes cancelled orders (and any other excluded status found in the data). List is in config. **[VERIFY]** whether revenue comes from a total column on orders or from sum of price x quantity over order_items. Freeze one definition |
| DD-7 | Tenant vs owner | Tenant is a store. No owner level (the dataset has none) |
| DD-8 | How freshness is tested | Unknown. Ask a mentor. We show our own probe using the simulator |
| DD-9 | What "10M records" means | 10M orders (dataset) |
| DD-10 | Second Postgres container allowed? | Assumed yes (the organiser PRD lists a separate analytics store as allowed). Fallback: separate database on the same server |
| DD-11 | Cache | None until a measurement shows it is needed |

## 6. Architecture

### 6.1 Components

```
 App / simulator ──> MAIN DB (Postgres 16, supplied tables, data untouched)
                      │  statement-level AFTER triggers (transition tables)
                      │  write signed change rows (+1 / -1) with xid8
                      ▼
                   OUTBOX table
                      │  worker reads in batches, safe-xid cursor
                      ▼
 WORKER ───────> ROLLUP DB (separate Postgres 16 container)
 (batch, fold,     ├─ rollup_orders_hour   (tenant, hour, status)
  one upsert/key)  ├─ rollup_product_day   (tenant, day, product, category)
                   ├─ worker_state         (cursor, snapshot, lag, heartbeat)
                   └─ quarantine           (rejected or odd rows)
                         ▲
 API (FastAPI) ──────────┘   dashboard queries never touch the main DB
   ├─ tenant auth (X-Tenant-Key → tenant)
   ├─ per-tenant concurrency limit (B-3)
   └─ /admin: rebuild (B-2), stats (B-5)
 BENCH HARNESS: speed, growth, freshness probe, DB safety, correctness
 PROOF PAGE (late extra): tenant view, scoreboard, flood button
```

### 6.2 Write path (capture)
1. The application (or simulator) writes orders and order items to the main DB as usual.
2. **Statement-level** AFTER INSERT / UPDATE / DELETE triggers on `orders` and `order_items` use transition tables and write **signed fact rows** to the outbox: sign +1 for new values, -1 for old values, plus the fields we aggregate on. Statement-level means a bulk update costs one trigger call, not one per row.
3. Each outbox row stores `xid8` of its writing transaction (`pg_current_xact_id()`).

### 6.3 Apply path (worker)
1. **Safe read.** The worker only reads outbox rows whose xid is below `pg_snapshot_xmin(pg_current_snapshot())`, ordered by `(xid, id)`. The cursor is the pair `(xid, id)`.
   *Why:* a plain increasing id is not safe. A transaction that started earlier can commit later and the worker would skip its rows forever. This was a bug in v0.1.
2. Fold the signed rows in memory by rollup key, so a bulk load does one upsert per key, not one per row.
3. Apply with `INSERT ... ON CONFLICT (keys) DO UPDATE SET count = rollup.count + EXCLUDED.count, sales = rollup.sales + EXCLUDED.sales`.
4. Save the new cursor **in the same rollup transaction**. A crash and retry cannot double count.
5. **Fail closed.** Rollup counts have `CHECK (count >= 0)`. If a batch would make a count negative, the transaction fails, the worker stops loudly, and the API reports `worker_stalled`. A wrong number is worse than a stale one.
6. Rows that cannot be parsed (missing tenant, unknown status, negative total, bad time) go to `quarantine` with a reason, and the batch continues.
7. Periodically delete outbox rows below the saved cursor to keep the main DB small.

### 6.4 Fallback if triggers are not allowed (DD-3)
The worker polls the main DB by an indexed `(updated_at, id)` cursor and recomputes the rollup for each touched hour straight from the main DB. This is slower to reach the freshness target, does not see deletes without a separate check, and is used only if triggers are refused. **[VERIFY]** an `updated_at` column exists before relying on this.

### 6.5 Initial load and rebuild (B-2)
Order of operations at setup:
1. Load the supplied data with `load.sh` and `post_load.sql`. No triggers exist yet, so the load is not slowed and nothing floods the outbox.
2. Create the outbox, triggers and functions.
3. Run the **rebuild procedure**: open a `REPEATABLE READ` snapshot on the main DB, aggregate all orders and items into fresh rollups, and store that snapshot in `worker_state`.
4. Start the worker. It skips outbox rows already visible in the stored snapshot (`pg_visible_in_snapshot`), so nothing is counted twice or missed.

The same procedure is B-2 (`POST /admin/rebuild`): build into shadow tables, compare, swap in. One tool, two jobs. The rebuild is a full scan of the main DB; we measure its cost and say so in the report.

### 6.6 Read path
- The API maps a key to a tenant and rejects any path that names another tenant.
- Count, trend and breakdown queries sum rollup rows for the range. Top-N sums the product rollup over the days in range, then sorts and limits.
- Every response includes `as_of`, `lag_seconds` (B-1) and `worker_stalled`.
- **No cache first.** We measure rollup query time. If it already meets the target, there is no cache. If we ever add one, the key includes tenant, range and a data version the worker bumps on write, so cached results can never be stale.

## 7. Data model

### 7.1 Source (supplied; names are placeholders until section 14 is filled)
Expected tables: `tenants`, `customers`, `products`, `orders`, `order_items`. Relationship: tenants → customers → orders → order_items → products. Real column names, types, keys and indexes come from `schema.sql`. We do not assume columns that are not present.

Placeholders used below: `<order_ts>` order time, `<status>` order status, `<revenue>` sales amount, `<category>` product category.

### 7.2 Rollup store
- `rollup_orders_hour(tenant_id, hour, status, order_count, revenue)`; primary key `(tenant_id, hour, status)`. Feeds count, totals, trend and by-status.
- `rollup_product_day(tenant_id, day, product_id, category, units, sales)`; primary key `(tenant_id, day, product_id)`. Feeds top-N and category breakdown. Only counted items are included (section 8, rule 3). Daily, not hourly, to limit rows.
- `worker_state(cursor_xid, cursor_id, snapshot, last_batch_at, lag_seconds, heartbeat)`.
- `quarantine(id, payload, reason, created_at)`.

Size check (*estimate*): hourly rollup is at most 100 tenants x 8,760 hours x about 4 statuses per year, a few million rows worst case and far fewer in practice. The product rollup is bounded by products per tenant (about 600 on average, *estimate* from 59,951 / 100) x days. Both are small next to 10M orders. We measure the real sizes and report them.

`orders_lite` from v0.1 is **removed**. Snapped ranges (section 8) make it unnecessary.

## 8. Frozen semantics

Decided now so tests can be exact.

1. **Ranges snap** to whole hours for count, trend and by-status, and to whole days for top-N. `from` inclusive, `to` exclusive, UTC.
2. **The response returns the snapped range** it actually used.
3. **Sales** exclude cancelled orders (and any other excluded status in the data). The excluded list is config. Order counts by status still show every status.
4. **Status change** moves the count between status rows (-1 old, +1 new). If the change flips an order between counted and not counted, its items are **fanned out**: the trigger emits -1 / +1 rows for each item so top-N stays right after a cancel.
5. **Delete** removes items first (FK RESTRICT on orders). Both emit negative rows.
6. **Time-bucket edits:** if an order's timestamp is edited, emit -1 on the old bucket and +1 on the new one.
7. **Tenant isolation:** every rollup key starts with `tenant_id` and every query filters on it. `X-Tenant-Key` is checked on every call. Wrong or missing key is 403.

## 9. API

All data endpoints are under `/tenants/{tenant_id}`. Header `X-Tenant-Key` is required. A key for tenant A used on tenant B's path returns 403. The count path matches the organiser example.

| Endpoint | Purpose | Requirement |
|---|---|---|
| `GET /tenants/{id}/orders/count?from&to` | Order count and total sales | FR-1 |
| `GET /tenants/{id}/orders/trend?from&to&grain=hour\|day` | Orders over time | FR-2 |
| `GET /tenants/{id}/products/top?from&to&n=10&by=sales\|units` | Top-N products | FR-3 |
| `GET /tenants/{id}/orders/by-status?from&to` | Breakdown by status | FR-4 |
| `GET /tenants/{id}/metrics/{name}?from&to` | Config-defined metric | B-4 (stretch) |

Every response: `{ "data": ..., "range": {"from": "...", "to": "..."}, "as_of": "...", "lag_seconds": 3.2, "worker_stalled": false }`.

Admin (separate admin key): `POST /admin/rebuild` (B-2), `GET /admin/stats` (B-5), `GET /health`.

Overload returns `429` with `Retry-After` (B-3). A tenant is limited to N concurrent heavy queries (per-tenant semaphore).

## 10. Phases

Hour 0 = 11:00 on 31 Oct. Hours are *estimates*; adjust to the real clock. Each phase has an **exit gate**: do not start the next phase until it passes, except where noted.

### Phase 0: Recon and data (H0 to H3)
- Fill the section 14 checklist from `schema.sql` and the CSV headers.
- Docker Compose skeleton: `main-db`, `rollup-db`, `worker`, `api`.
- Load the 1M set, then the 10M set, with the supplied `load.sh` and `post_load.sql`. Run `validate_data.py` and save the output.
- Record **baseline**: how slow the naive direct queries are at 1M and 10M, with the supplied indexes.
- Send the mentor questions (section 16). Do not wait for answers.
- **Gate:** data loaded, baseline numbers saved, section 14 filled, names in section 7 replaced with real ones.

### Phase 1: Core pipeline (H3 to H8)
- Outbox, statement-level triggers, worker with signed rows, safe-xid cursor, rollups.
- Measure trigger overhead on writes **now**, not at the end.
- Four query endpoints with tenant key enforcement.
- Updates (status change), fan-out to items, and deletes handled.
- **Gate:** all four endpoints return exact results on 1M rows for 10 sample tenants and ranges, including after an update.

### Phase 2: Trust and measurement (H8 to H11)
- Correctness suite: golden comparison of every endpoint against independent plain SQL on the main DB.
- Tenant isolation, idempotency, out-of-order commit, kill-the-worker-mid-batch tests.
- Benchmark harness v1: query speed, growth, freshness probe, DB safety.
- Run the full benchmark at 10M.
- **Gate:** all queries match exactly; Good targets pass at 10M.
- **Checkpoint:** record a backup demo video of the working core.

### Phase 3: Bonuses (H11 to H14)
- B-1 data age in responses (mostly done in phase 1).
- B-2 rebuild from scratch, verified equal to the live numbers.
- B-3 per-tenant concurrency limit with 429.
- B-5 monitoring page: query time, lag, quarantine count.
- B-4 metrics from a config file (last; skip if late). Example:
```yaml
metrics:
  - name: orders_by_status
    source: orders
    time_field: <order_ts>
    granularity: hour
    group_by: [<status>]
    measures:
      - { fn: count }
      - { fn: sum, field: <revenue> }
```
- **Gate:** each bonus has a test that passes.

### Phase 4: Standout features (H14 to H19)
Only if phases 0 to 3 are done and tested. Build in this order; each is a feature flag that cannot break the core.
1. **One-command scoreboard:** `docker compose up`, then the bench command. It prints each organiser target with Good and Great status, the 1M vs 10M ratio, and the hardware used.
2. **Try-to-break-it test:** random inserts, updates and cancels over random tenants and ranges; compare with plain SQL; show a mismatch counter. Include a chaos run that kills the worker and restarts it.
3. **Freshness probe:** write via the simulator, time until the change appears; report p50 and p95.
4. **Live proof page:** the same tenant query run directly and through the layer, with timings and main-DB load. A "flood" button hammers one large tenant while a normal app workload graph stays flat.
5. **Noisy-neighbor demo:** with B-3, show a small tenant's p95 stays stable while a large tenant floods.

### Phase 5: Freeze and delivery (H19 to H24)
- **Feature freeze at H19.**
- Clean-clone test: a new machine can run it from the README.
- Final benchmark with the laptop plugged in and nothing else open.
- README: endpoints, how to run tests and benchmark, assumptions, limits.
- Architecture diagram, short "what we did not do" slide, demo rehearsed three times, backup video.

**Cut order if behind:** B-4, noisy-neighbor demo, flood button, B-5 page. Then report the scale actually measured, honestly.

## 11. Test plan

| Test | What it proves |
|---|---|
| Dataset sanity | `validate_data.py` output saved and rerun by us |
| Golden correctness | Every endpoint equals independent plain SQL on the main DB, across many tenants and ranges, including edge ranges (empty, one hour, month boundary) |
| Update handling | Create, ship, deliver, cancel via the simulator; after each step the layer equals plain SQL; delete and re-insert too |
| Tenant isolation | Wrong key returns 403; no row from another tenant appears; sum of all tenant rollups equals the global SQL total |
| Idempotency | Replaying the same outbox batch changes nothing |
| Out-of-order commit | Two overlapping transactions commit in reverse order; nothing is skipped |
| Crash recovery | Killing the worker mid-batch and restarting still gives exact numbers |
| Rebuild equality | Rebuilt rollups equal the live rollups |
| Fuzz and chaos | Random operations with random worker kills leave zero mismatches |
| Quarantine | A malformed row is isolated, not fatal |
| Fail closed | A forced negative count stops the worker and sets `worker_stalled` |

## 12. Benchmark methodology

- Print data size, hardware, operating system and whether the laptop was plugged in at the top of every result.
- **Query speed:** each dashboard query, 5 warm-up runs then 30 timed runs; report p50, p95, max.
- **Naive baseline:** the same queries straight on the main DB with the same indexes. Shows what the layer buys.
- **Growth:** same queries at 1M and 10M, same 100 tenants in both; report the ratio.
- **Freshness:** 50 probes; write via the simulator, poll the API until the change appears; the simulator's commit-time log gives the start.
- **Database safety, three runs** using the simulator's OLTP mode: (a) no layer at all, (b) triggers and worker running, (c) layer running plus dashboard query load. Slowdown is (b) and (c) against (a).
- **Correctness:** percentage of compared queries that match exactly.
- Run on Vignesh's Lenovo (i7-13620H, 16 GB), plugged in. Do not compare results taken on different machines. Do not state scale we did not run.

## 13. Environment

- Docker Compose with `main-db`, `rollup-db`, `worker`, `api`. One command to start. Healthchecks so the worker waits for both DBs.
- Windows 11: use **named Docker volumes**, not bind mounts (bind mounts are slow). Cap WSL2 memory near 12 GB so Windows keeps some room.
- Pin Postgres 16 and the Python dependencies.
- Vignesh's laptop runs the heavy parts (databases, load, benchmark). Balu's laptop runs API, tests, README.

## 14. Verify before writing SQL (30-minute reconciliation)

Open `schema.sql` and the CSV headers, then fill these in and replace the placeholders in sections 7 and 9.

- [ ] Order time column and its type (`<order_ts>`, timestamp or timestamptz)
- [ ] Exact status values in the data (`<status>`); expected placed, shipped, delivered, cancelled; check for others
- [ ] Revenue definition (`<revenue>`): total column on orders, or price x quantity over items (DD-6)
- [ ] `tenant_id` present on orders, order_items, products, customers (DD-2)
- [ ] Category column on products (`<category>`) and how many distinct values
- [ ] Primary keys, foreign keys, and indexes after `post_load.sql`
- [ ] Is there an updated-at column on orders (needed only for the polling fallback)
- [ ] Does the simulator touch anything besides orders
- [ ] Size of the largest tenant (noisy-neighbor test)
- [ ] Anything odd that should reach quarantine

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Trigger overhead slows main DB writes by more than 10% | Statement-level triggers, narrow outbox rows, measure in phase 1; polling fallback (section 6.4) |
| Rebuild is a heavy full scan | Run once at setup, report the cost, expose it as B-2 only |
| Worker falls behind under a flood | Larger batches, lag shown in every response, `worker_stalled` flag |
| Schema differs from assumptions | Phase 0 gate and section 14 before any SQL |
| Judges use their own ranges or queries | Whole-hour and whole-day snapping documented in README; response echoes the range; ask the mentor |
| Second Postgres container not accepted | Ask a mentor; fallback is a separate database on the same server |
| Judges load their own data | Loader reads the same CSV format; section 14 keeps names configurable |
| Laptop noise in benchmarks | One machine, plugged in, repeated runs, hardware stated |
| Standout work breaks the core | Feature flags, branches, freeze at H19 |
| A teammate cannot explain a module | Swap-and-explain sessions at H10 and H18 |

## 16. Questions for mentors (send now, do not wait)

1. Do judges run our API with their own ranges, or only the README examples?
2. How exactly is freshness tested: their writer, or can we show our simulator?
3. Are whole-hour and whole-day snapped ranges acceptable?
4. How are bonus items scored?
5. Is a second Postgres container fine, and may we add triggers and tables to the main DB?
6. Is Sarvam use required or scored?

## 17. Definition of done

- FR-1 to FR-7 pass; B-1, B-2, B-3, B-5 pass; B-4 if time allowed.
- Golden correctness test: zero mismatches.
- Benchmark report shows each target with measured numbers, data size and hardware.
- `docker compose up` works from a clean clone using only the README.
- README notes the data is synthetic (Olist-patterned) and lists known limits.
- Backup video recorded; limits slide written; both members can explain every module.

## 18. Team and process

| Role | Owns |
|---|---|
| Engine owner (Vignesh, Lenovo, heavier machine) | Databases, loading, outbox, triggers, worker, rollups, benchmark runs |
| API owner (Balu, second laptop) | API, tenant auth, tests, proof page, README |

Both review each other's code. Commit small, merge often, one branch per phase.

## 19. Change log

- **v1.0:** domain locked to e-commerce. Supplied dataset replaces our own generator. Added dataset and simulator section. Added xid-based safe cursor, signed fact rows, statement-level triggers, fail-closed check, item fan-out on status change. Replaced `orders_lite` and exact edge lookups with snapped ranges. Dropped the review page, security-events reuse and Sarvam feature. Freshness target tightened to p95 under 5 s. Resolved most open decisions.
- **v0.1:** first draft, dataset unknown.
