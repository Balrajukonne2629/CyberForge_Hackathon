# Dashboard API Specification

Complete reference for all API endpoints, authentication headers, request parameters, and response shapes used by the Multi-Tenant Aggregation Layer Demo Dashboard.

---

## 1. Authentication & Security Model

- **Tenant Authentication (`X-Tenant-Key`)**:
  - Required on all `/tenants/{tenant_id}/*` endpoints.
  - Matches the key registered in `tenant_registry` (`api_key` column, e.g. `tenant-key-1` for tenant 1).
  - The master `ADMIN_KEY` is also accepted as a bypass on tenant endpoints.
  - Mismatched or missing key returns `403 Forbidden`.
- **Admin Authentication (`X-Admin-Key`)**:
  - Required on all `/admin/*` and `/demo/*` endpoints.
  - Configured via `config.yaml` (`auth.admin_key`, default `admin-secret-key-12345`).
  - Mismatched or missing key returns `403 Forbidden`.
- **Demo Mode Guard (`DEMO_MODE`)**:
  - All `/demo/*` endpoints require the environment variable `DEMO_MODE=1`.
  - When `DEMO_MODE != "1"`, all `/demo/*` endpoints return `404 Not Found`.

---

## 2. Core Tenant Endpoints

### 2.1 GET `/tenants/{tenant_id}/orders/count`
Returns aggregate order count and total revenue within the snapped hour window.

- **Headers**: `X-Tenant-Key: <key>`
- **Query Parameters**:
  - `from` (string, ISO 8601, e.g. `2026-10-01T00:00:00Z`): snaps down to hour boundary.
  - `to` (string, ISO 8601, e.g. `2026-10-30T00:00:00Z`): snaps up to hour boundary.
- **Response** (`200 OK`):
```json
{
  "data": {
    "order_count": 10000,
    "total_sales": 1495200.50
  },
  "range": {
    "from": "2026-10-01T00:00:00Z",
    "to": "2026-10-30T00:00:00Z"
  },
  "as_of": "2026-11-01T00:00:00.000000+00:00",
  "lag_seconds": 0.0,
  "worker_stalled": false
}
```

### 2.2 GET `/tenants/{tenant_id}/orders/trend`
Returns timeseries buckets for order counts and sales.

- **Headers**: `X-Tenant-Key: <key>`
- **Query Parameters**:
  - `from` (string, ISO 8601)
  - `to` (string, ISO 8601)
  - `grain` (string, optional: `"hour"` | `"day"`, default `"hour"`)
- **Response** (`200 OK`):
```json
{
  "data": [
    {
      "bucket": "2026-10-25T10:00:00Z",
      "order_count": 42,
      "sales": 5820.10
    }
  ],
  "range": {
    "from": "2026-10-25T00:00:00Z",
    "to": "2026-10-26T00:00:00Z"
  },
  "as_of": "2026-11-01T00:00:00.000000+00:00",
  "lag_seconds": 0.0,
  "worker_stalled": false
}
```

### 2.3 GET `/tenants/{tenant_id}/products/top`
Returns top $N$ products ordered by sales or units sold within the snapped day window.

- **Headers**: `X-Tenant-Key: <key>`
- **Query Parameters**:
  - `from` (string, ISO 8601): snaps down to day boundary.
  - `to` (string, ISO 8601): snaps up to day boundary.
  - `n` (int, optional, 1-100, default 10)
  - `by` (string, optional: `"sales"` | `"units"`, default `"sales"`)
- **Response** (`200 OK`):
```json
{
  "data": [
    {
      "product_id": 402,
      "category": "Electronics",
      "units": 150,
      "sales": 18500.00
    }
  ],
  "range": {
    "from": "2026-10-01T00:00:00Z",
    "to": "2026-10-30T00:00:00Z"
  },
  "as_of": "2026-11-01T00:00:00.000000+00:00",
  "lag_seconds": 0.0,
  "worker_stalled": false
}
```

### 2.4 GET `/tenants/{tenant_id}/orders/by-status`
Returns breakdown of orders and revenue grouped by status.

- **Headers**: `X-Tenant-Key: <key>`
- **Query Parameters**:
  - `from` (string, ISO 8601)
  - `to` (string, ISO 8601)
- **Response** (`200 OK`):
```json
{
  "data": [
    {
      "status": "delivered",
      "order_count": 8900,
      "sales": 1340000.00
    },
    {
      "status": "canceled",
      "order_count": 500,
      "sales": 0.00
    }
  ],
  "range": {
    "from": "2026-10-01T00:00:00Z",
    "to": "2026-10-30T00:00:00Z"
  },
  "as_of": "2026-11-01T00:00:00.000000+00:00",
  "lag_seconds": 0.0,
  "worker_stalled": false
}
```

### 2.5 GET `/tenants/{tenant_id}/stream` (Server-Sent Events)
Live push stream of updates via PostgreSQL `NOTIFY rollup_updated`.

- **Headers**: `X-Tenant-Key: <key>`
- **Media Type**: `text/event-stream`
- **Initial Event & Mutation Events**:
```text
data: {"as_of": "2026-11-01T00:00:00Z", "lag_seconds": 0.0, "worker_stalled": false, "changed_buckets": [{"hour": "2026-10-30T14:00:00", "status": "delivered", "count": 15, "revenue": 1820.50}]}

: keepalive
```

---

## 3. Platform & System Admin Endpoints

### 3.1 GET `/health`
Global health status of the aggregation pipeline (public / unauthenticated).

- **Response** (`200 OK`):
```json
{
  "status": "healthy",
  "worker_stalled": false,
  "total_tenants": 100,
  "stalled_tenants_count": 0,
  "stalled_tenant_ids": [],
  "max_lag_seconds": 0.0,
  "as_of": "2026-11-01T00:00:00Z"
}
```

### 3.2 GET `/admin/stats`
Per-tenant operational telemetry and worker state.

- **Headers**: `X-Admin-Key: <key>`
- **Response** (`200 OK`):
```json
{
  "total_tenants": 100,
  "stalled_count": 0,
  "max_lag_seconds": 0.0,
  "tenants": {
    "1": {
      "database_name": "tenant_001",
      "lag_seconds": 0.0,
      "stalled": false,
      "last_error": null,
      "heartbeat": "2026-11-01T00:00:00Z"
    }
  }
}
```

### 3.3 GET `/admin/platform/summary`
Sums rollup tables across all 100 tenants with no tenant filter.

- **Headers**: `X-Admin-Key: <key>`
- **Query Parameters**:
  - `from` (string, ISO 8601)
  - `to` (string, ISO 8601)
- **Response** (`200 OK`):
```json
{
  "totals": {
    "order_count": 1000000,
    "total_sales": 149520000.00
  },
  "trend": [
    {
      "bucket": "2026-10-25T10:00:00Z",
      "order_count": 4200,
      "sales": 582000.00
    }
  ],
  "top_tenants": [
    {
      "tenant_id": 1,
      "order_count": 35000,
      "sales": 5200000.00
    }
  ],
  "status_split": [
    {
      "status": "delivered",
      "order_count": 890000,
      "sales": 134000000.00
    }
  ],
  "quarantine_count": 0,
  "stalled_tenants_count": 0,
  "max_lag_seconds": 0.0,
  "range": {
    "from": "2026-10-01T00:00:00Z",
    "to": "2026-10-30T00:00:00Z"
  },
  "as_of": "2026-11-01T00:00:00Z"
}
```

### 3.4 GET `/admin/platform/scaling`
Loads benchmark scaling metrics from `docs/results/scaling.json` if available.

- **Response** (`200 OK` or `404 Not Found` if missing):
```json
{
  "status": "available",
  "data": { ... }
}
```

### 3.5 POST `/admin/rebuild`
Triggers rebuild of rollups from raw tables.

- **Headers**: `X-Admin-Key: <key>`
- **Query Parameters**: `tenant_id` (optional, omits to rebuild all)

---

## 4. Demo Controls (`DEMO_MODE=1` and `X-Admin-Key`)

### 4.1 POST `/demo/order`
Inserts 1 delivered order with 1-3 items into specified tenant DB with ID in 900,000,000+ range.

- **Headers**: `X-Admin-Key: <key>`
- **Request Body**: `{"tenant_id": 1}`
- **Response** (`200 OK`):
```json
{
  "status": "created",
  "tenant_id": 1,
  "order_id": 900000001,
  "items_count": 2,
  "total": 234.50,
  "commit_ts": "2026-11-01T00:00:01.123456Z"
}
```

### 4.2 POST `/demo/cancel`
Cancels the latest demo order (`order_id >= 900000000`) for the specified tenant.

- **Headers**: `X-Admin-Key: <key>`
- **Request Body**: `{"tenant_id": 1}`
- **Response** (`200 OK`):
```json
{
  "status": "canceled",
  "tenant_id": 1,
  "order_id": 900000001,
  "commit_ts": "2026-11-01T00:00:05.123456Z"
}
```

### 4.3 POST `/demo/flood` & POST `/demo/flood/stop`
Starts/stops simulated batch order ingestion for `tenant_id` at rate `n` orders/second (max 60 seconds).

- **Headers**: `X-Admin-Key: <key>`
- **POST `/demo/flood` Body**: `{"tenant_id": 1, "n": 20}`
- **POST `/demo/flood/stop` Body**: `{}`

### 4.4 GET `/demo/naive/{tenant_id}`
Executes the naive 30-day top-10 products query directly on the tenant's raw DB with `statement_timeout = 5s`.

- **Headers**: `X-Admin-Key: <key>`
- **Response** (`200 OK`):
```json
{
  "tenant_id": 1,
  "elapsed_ms": 142.85,
  "rows_count": 10,
  "rows": [
    {"product_id": 402, "category": "Electronics", "units": 150, "sales": 18500.00}
  ]
}
```

### 4.5 POST `/demo/reset`
Deletes all demo records (`order_id >= 900000000`) across all tenant databases, restoring clean dataset so the integrity tripwire passes.

- **Headers**: `X-Admin-Key: <key>`
- **Response** (`200 OK`):
```json
{
  "status": "reset_complete",
  "deleted_orders": 4,
  "deleted_items": 8
}
```

---

## 5. Scaling Benchmark Specification Schema

The Scale tab reads `docs/results/scaling.json`. The normalized schema representation is:

```json
{
  "date": "2026-10-31T19:17:58Z",
  "hardware": "12 CPUs, win32, max_connections=300",
  "poll": [
    {"tenants": 1, "p50_ms": 25.9, "p95_ms": 37.4},
    {"tenants": 10, "p50_ms": 29.4, "p95_ms": 38.2},
    {"tenants": 50, "p50_ms": 30.7, "p95_ms": 36.8},
    {"tenants": 100, "p50_ms": 27.6, "p95_ms": 43.9}
  ],
  "event": [
    {"tenants": 1, "p50_ms": 26.8, "p95_ms": 39.0},
    {"tenants": 10, "p50_ms": 27.5, "p95_ms": 39.0},
    {"tenants": 50, "p50_ms": 26.5, "p95_ms": 29.6},
    {"tenants": 100, "p50_ms": 26.6, "p95_ms": 39.3}
  ],
  "idle": {
    "main_db_commits_per_sec": 30.15,
    "rollup_db_commits_per_sec": 1.75,
    "cpu_usage_cores": 0.05
  }
}
```
If `docs/results/scaling.json` does not exist, the dashboard displays `"not measured yet"`.
