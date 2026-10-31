#!/usr/bin/env python3
"""Cross-platform fast dataset loader for PostgreSQL using psycopg2 COPY streaming."""
import sys, os, time, gzip, psycopg2

def load_data(data_dir: str, db_url: str):
    print(f"=== Loading dataset from {data_dir} into {db_url} ===")
    t_start = time.time()
    
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cur = conn.cursor()

    # 1. Run schema.sql
    schema_path = os.path.join(os.path.dirname(data_dir), "schema.sql")
    if not os.path.exists(schema_path):
        schema_path = "data/mitigata_dataset/schema.sql"
    print(f"Executing {schema_path}...")
    with open(schema_path, "r", encoding="utf-8") as f:
        cur.execute(f.read())

    # 2. Load tables from CSV.GZ
    tables = ["tenants", "products", "customers", "orders", "order_items"]
    for t in tables:
        gz_path = os.path.join(data_dir, f"{t}.csv.gz")
        print(f"Loading {t} from {gz_path}...")
        t0 = time.time()
        with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
            cur.copy_expert(f"COPY {t} FROM STDIN WITH CSV HEADER", gz)
        print(f"  Loaded {t} in {time.time() - t0:.2f}s")

    # 3. Run post_load.sql
    post_load_path = os.path.join(os.path.dirname(data_dir), "post_load.sql")
    if not os.path.exists(post_load_path):
        post_load_path = "data/mitigata_dataset/post_load.sql"
    print(f"Executing {post_load_path}...")
    t0 = time.time()
    with open(post_load_path, "r", encoding="utf-8") as f:
        cur.execute(f.read())
    print(f"  Indexes and ANALYZE completed in {time.time() - t0:.2f}s")

    # 4. Summary counts
    cur.execute("SELECT 'orders', count(*) FROM orders UNION ALL SELECT 'items', count(*) FROM order_items")
    counts = cur.fetchall()
    print("Verification counts:", counts)
    print(f"=== Total load time: {time.time() - t_start:.2f}s ===\n")
    cur.close()
    conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python load_dataset.py <data_dir> <db_url>")
        sys.exit(1)
    load_data(sys.argv[1], sys.argv[2])
