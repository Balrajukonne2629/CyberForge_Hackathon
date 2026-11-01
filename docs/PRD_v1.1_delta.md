# PRD v1.1 Delta: Per-Tenant Database Architecture (Phase 2.5)

## 1. Context & Mandatory Requirement
Per mentor directive, the single shared `shop` database architecture must change to a strict per-tenant database layout. The original PRD states that each tenant's orders, products, and customers reside in their own distinct database. This document records the architectural decisions, migration invariants, and schema contract for Phase 2.5.

The git tag `phase2-single-db` marks the state of the codebase before this migration. The single-DB path is retained under configuration profile (`layout: single`) for baseline comparisons, while the official submission operates under `layout: per_tenant`.

---

## 2. Frozen Architectural Decisions

### 2.1 Storage Layout (Main DB Container)
- **Host**: A single PostgreSQL instance (`cyberforge-main-db` container on port 5432).
- **Tenant Databases**: 100 separate databases:
  - Profile 1M: `tenant_001` through `tenant_100`.
  - Profile 10M: `t10m_001` through `t10m_100`.
- **Tenant Schema**: Identical schema across all 100 tenant databases (`tenants`, `customers`, `products`, `orders`, `order_items`).
- **Column Invariant**: The `tenant_id` column is preserved in all tables within each tenant DB. This ensures existing SQL queries, triggers, foreign keys, and golden parity test queries remain valid without modification.
- **Connection Capacity**: PostgreSQL `max_connections` raised from 100 to 300 in `docker-compose.yml` to support concurrent persistent connections across all tenant databases.

### 2.2 Central Rollup Database
- **Host**: `cyberforge-rollup-db` container on port 5433 (`rollup_1m` / `rollup_10m`).
- **Rollup Tables**: `rollup_orders_hour` and `rollup_product_day` remain unchanged in shape. Every primary key and query dimension continues to begin with `tenant_id`.
- **Tenant Registry**: Added `tenant_registry` table:
  - Columns: `tenant_id` (INT PK), `database_name` (TEXT), `api_key` (TEXT), `created_at` (TIMESTAMP).
- **Worker State**: Replaced single-row `worker_state` with per-tenant rows:
  - Columns: `tenant_id` (INT PK), `cursor_xid` (xid8), `cursor_id` (BIGINT), `snapshot` (pg_snapshot), `lag_seconds` (NUMERIC(10,2)), `heartbeat` (TIMESTAMP), `stalled` (BOOLEAN), `last_error` (TEXT), `has_pending` (BOOLEAN), `oldest_pending_ts` (TIMESTAMP).
- **Quarantine**: Preserves `tenant_id` column to attribute quarantined outbox events to their originating tenant.

### 2.3 Outbox & Statement-Level Triggers
- **Location**: The `outbox` table and statement-level triggers reside **inside each tenant database**.
- **Reason**: PostgreSQL triggers cannot write across database boundaries. Each tenant database self-contains its own transaction-bound outbox stream.

### 2.4 Per-Tenant Fail-Closed Semantics
- **Blast Radius Isolation**: If an outbox delta for tenant *T* violates an integrity constraint (e.g., negative count or units check constraint on rollup tables):
  - Ingestion halts **only for tenant *T***.
  - The worker sets `worker_state.stalled = true` and `last_error` for tenant *T*.
  - All other 99 tenants continue ingesting and updating rollups without interruption.
  - API responses for tenant *T* return `worker_stalled: true`.
  - Global `/health` endpoint reports overall status (`stalled` if any tenant is stalled) and provides counts and IDs of stalled tenants.

### 2.5 Multi-Source Worker Pipeline
- **Round-Robin Scheduling**: The worker maintains persistent connections to tenant databases, visiting each tenant round-robin.
- **Fairness & Starvation Prevention**: Rows read per tenant per visit are capped (`per_tenant_batch = 500`), designed so that a high-volume flood on one tenant cannot starve other tenants.
- **Worker Sharding**: Sharding by environment variables `WORKER_ID` and `WORKER_COUNT`:
  `tenant_id % WORKER_COUNT == WORKER_ID`.

### 2.6 Per-Tenant Rebuild
- **Granular Rebuild**: Supports rebuilding rollups for a single tenant `tenant_id` using a `REPEATABLE READ` snapshot against that tenant's database.
- **Parallel Full Rebuild**: Full system rebuild parallelizes per-tenant rebuilds across tenant worker threads.
- **Admin API**: `POST /admin/rebuild?tenant_id=...` and `POST /admin/rebuild` (full).
