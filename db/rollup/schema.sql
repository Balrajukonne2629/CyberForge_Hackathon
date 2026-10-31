-- Rollup database schema for analytics store (v1.1 per-tenant multi-source)

CREATE TABLE IF NOT EXISTS rollup_orders_hour (
    tenant_id int NOT NULL,
    hour timestamp NOT NULL,
    status text NOT NULL,
    order_count int NOT NULL DEFAULT 0 CHECK (order_count >= 0),
    revenue numeric(14,2) NOT NULL DEFAULT 0.00,
    PRIMARY KEY (tenant_id, hour, status)
);

CREATE INDEX IF NOT EXISTS idx_rollup_orders_hour_range 
    ON rollup_orders_hour (tenant_id, hour);

CREATE TABLE IF NOT EXISTS rollup_product_day (
    tenant_id int NOT NULL,
    day timestamp NOT NULL,
    product_id bigint NOT NULL,
    category text NOT NULL,
    units int NOT NULL DEFAULT 0 CHECK (units >= 0),
    sales numeric(14,2) NOT NULL DEFAULT 0.00,
    PRIMARY KEY (tenant_id, day, product_id)
);

CREATE INDEX IF NOT EXISTS idx_rollup_product_day_range 
    ON rollup_product_day (tenant_id, day);

CREATE TABLE IF NOT EXISTS tenant_registry (
    tenant_id int PRIMARY KEY,
    database_name text NOT NULL,
    api_key text NOT NULL,
    created_at timestamp NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS worker_state (
    tenant_id int PRIMARY KEY,
    cursor_xid xid8 NOT NULL DEFAULT '0'::xid8,
    cursor_id bigint NOT NULL DEFAULT 0,
    snapshot pg_snapshot,
    last_batch_at timestamp,
    lag_seconds numeric(10,2) NOT NULL DEFAULT 0.0,
    heartbeat timestamp NOT NULL DEFAULT clock_timestamp(),
    stalled boolean NOT NULL DEFAULT false,
    last_error text,
    has_pending boolean NOT NULL DEFAULT false,
    oldest_pending_ts timestamp
);

CREATE TABLE IF NOT EXISTS quarantine (
    id bigserial PRIMARY KEY,
    tenant_id int,
    outbox_id bigint,
    payload jsonb NOT NULL,
    reason text NOT NULL,
    created_at timestamp NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_quarantine_tenant ON quarantine (tenant_id);
