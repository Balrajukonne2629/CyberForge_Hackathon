-- Rollup database schema for analytics store

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

CREATE TABLE IF NOT EXISTS worker_state (
    id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    cursor_xid xid8 NOT NULL DEFAULT '0'::xid8,
    cursor_id bigint NOT NULL DEFAULT 0,
    snapshot pg_snapshot,
    last_batch_at timestamp,
    lag_seconds numeric(10,2) NOT NULL DEFAULT 0.0,
    heartbeat timestamp NOT NULL DEFAULT clock_timestamp(),
    worker_stalled boolean NOT NULL DEFAULT false,
    stalled_reason text,
    has_pending boolean NOT NULL DEFAULT false,
    oldest_pending_ts timestamp
);

-- Initialize default single-row state
INSERT INTO worker_state (id, cursor_xid, cursor_id, heartbeat, worker_stalled)
VALUES (1, '0'::xid8, 0, clock_timestamp(), false)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS quarantine (
    id bigserial PRIMARY KEY,
    outbox_id bigint,
    payload jsonb NOT NULL,
    reason text NOT NULL,
    created_at timestamp NOT NULL DEFAULT clock_timestamp()
);
