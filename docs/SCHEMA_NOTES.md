# Schema Notes: Mitigata CyberForge Dataset Reconciliation

Reconciliation performed against `schema.sql`, `post_load.sql`, `simulator.py`, and raw CSV headers/data in `data/mitigata_dataset/`.

## Section 14 Checklist

| Item | Found in Data / Schema | Notes & Verification |
|---|---|---|
| **Order time column (`<order_ts>`)** | `created_at` | Type is `timestamp NOT NULL` (without timezone; UTC semantics per DD-5). |
| **Exact status values (`<status>`)** | 8 distinct statuses: `delivered`, `shipped`, `canceled`, `unavailable`, `invoiced`, `processing`, `created`, `approved` | Note single 'l' spelling in `canceled`. Counts in 1M slice: `delivered`: 970,110, `shipped`: 11,245, `canceled`: 6,302, `unavailable`: 6,167, `invoiced`: 3,180, `processing`: 2,925, `created`: 46, `approved`: 25. |
| **Revenue definition (`<revenue>`)** | `orders.total` (numeric(14,2)) AND `order_items.qty * order_items.price` | Exact match: 0 discrepancies across 87,595 checked sample orders. For order-level rollups, `orders.total` is used. For product-level rollups, `order_items.qty * order_items.price` is used. |
| **`tenant_id` present on all tables** | **YES** | Confirmed present on `tenants`, `customers`, `products`, `orders`, and `order_items`. No derivation required for `order_items`. |
| **Category column (`<category>`)** | `products.category` | Type is `text NOT NULL`. Exactly 72 distinct categories (e.g., `sports_leisure`, `bed_bath_table`, `watches_gifts`, `computers_accessories`, etc.). |
| **Primary Keys** | `tenants(tenant_id)`<br>`customers(customer_id)`<br>`products(product_id)`<br>`orders(order_id)`<br>`order_items(item_id)` | All integer/bigint single-column primary keys. |
| **Foreign Keys (post_load.sql)** | `orders.tenant_id -> tenants.tenant_id`<br>`products.tenant_id -> tenants.tenant_id`<br>`customers.tenant_id -> tenants.tenant_id` | Note: FK from `order_items` to `orders` is not defined in `post_load.sql` but maintained cleanly in dataset. |
| **Baseline Indexes (post_load.sql)** | `orders_tenant_created ON orders (tenant_id, created_at)`<br>`items_order ON order_items (order_id)`<br>`items_tenant_product ON order_items (tenant_id, product_id)` | Present and indexed for baseline queries. |
| **Updated-at column** | `orders.updated_at` (`timestamp NOT NULL`) | Present and indexed by application if fallback polling is needed. Validated `updated_at >= created_at`. |
| **Simulator behavior** | Touches `orders` and `order_items` | Simulator `writes` mode inserts orders and order items; updates `orders.status` and `orders.updated_at`. Simulator `oltp` mode reads `orders JOIN order_items` and updates `orders.updated_at`. |
| **Largest tenant distribution** | Tenant 1 is largest (~19.34% of orders) | Tenant 1: 193,395 orders (1M). Top 5: Tenant 1 (193k), Tenant 2 (96k), Tenant 3 (64k), Tenant 4 (48k), Tenant 5 (38k). Bottom tenants have ~1,800 to 2,000 orders. |
| **Quarantine criteria** | Violations captured by worker | Null `tenant_id` or `order_id`, non-positive `total` or `price`/`qty`, unknown status, invalid timestamps (`updated_at < created_at`), or tenant mismatch between order and items. |

## Differences from PRD Assumptions

1. **Status spelling:** The PRD mentions `cancelled` (double l), but the raw dataset and simulator use American English `canceled` (single l). Excluded status list in config is set to `["canceled", "unavailable"]`.
2. **`order_items.tenant_id`:** The PRD flagged `[VERIFY]` whether `tenant_id` is on `order_items`. It IS present natively on `order_items`, so no join to `orders` is needed to identify the tenant.
3. **Revenue consistency:** Both `orders.total` and item sum (`qty * price`) match cleanly down to the cent.
