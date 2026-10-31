#!/usr/bin/env python3
"""scripts/split_by_tenant.py:
Stream supplied CSV.GZ files, create a template DB with schema,
clone it per tenant, COPY each tenant's rows into its own database,
run post_load.sql and ANALYZE per tenant DB in parallel.
Never modifies the original data files.
Populates tenant_registry and worker_state in rollup-db.
"""
import sys, os, time, gzip, shutil, argparse
from concurrent.futures import ThreadPoolExecutor
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_name, get_tenant_db_url

def terminate_connections(cur, db_name: str):
    cur.execute("""
        SELECT pg_terminate_backend(pid) 
        FROM pg_stat_activity 
        WHERE datname = %s AND pid <> pg_backend_pid()
    """, (db_name,))

def split_and_load(profile: str = "1m", tenant_count: int = 100, max_workers: int = 16):
    cfg = get_config(profile)
    t_start = time.time()
    print("=" * 70)
    print(f"SPLITTING & LOADING DATASET PER-TENANT (PROFILE: {profile.upper()})")
    print(f"Tenants: {tenant_count} databases | Parallel workers: {max_workers}")
    print("=" * 70)

    data_dir = "data/mitigata_dataset/small" if profile == "1m" else "data/mitigata_dataset/main"
    schema_sql_path = "data/mitigata_dataset/schema.sql"
    post_load_sql_path = "data/mitigata_dataset/post_load.sql"
    staging_dir = f"data/.split_staging_{profile}"

    if not os.path.exists(data_dir):
        raise FileNotFoundError(f"Data directory not found: {data_dir}")

    with open(schema_sql_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    with open(post_load_sql_path, "r", encoding="utf-8") as f:
        post_load_sql = f.read()

    template_db = f"template_tenant_{profile}"
    admin_conn_url = "postgresql://postgres:postgres@localhost:5432/postgres"

    # 1. Create Template DB
    print(f"\n[1/5] Creating template database: {template_db} ...")
    t0 = time.time()
    conn = psycopg2.connect(admin_conn_url)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    terminate_connections(cur, template_db)
    cur.execute(f"DROP DATABASE IF EXISTS {template_db}")
    cur.execute(f"CREATE DATABASE {template_db}")
    cur.close()
    conn.close()

    tmpl_conn = psycopg2.connect(f"postgresql://postgres:postgres@localhost:5432/{template_db}")
    tmpl_conn.autocommit = True
    tmpl_cur = tmpl_conn.cursor()
    tmpl_cur.execute(schema_sql)
    tmpl_cur.close()
    tmpl_conn.close()
    print(f"  Template database created in {time.time() - t0:.2f}s")

    # 2. Clone Template DB for each tenant
    print(f"\n[2/5] Cloning {tenant_count} tenant databases from {template_db} ...")
    t0 = time.time()
    conn = psycopg2.connect(admin_conn_url)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    for tid in range(1, tenant_count + 1):
        db_name = get_tenant_db_name(tid, profile)
        terminate_connections(cur, db_name)
        cur.execute(f"DROP DATABASE IF EXISTS {db_name}")
        cur.execute(f"CREATE DATABASE {db_name} TEMPLATE {template_db}")
        if tid % 25 == 0 or tid == tenant_count:
            print(f"  Cloned up to {db_name} ({tid}/{tenant_count})")
    cur.close()
    conn.close()
    print(f"  All {tenant_count} databases cloned in {time.time() - t0:.2f}s")

    # 3. Stream and demux CSV.GZ into tenant staging files
    print(f"\n[3/5] Streaming CSVs from {data_dir} into tenant staging files ...")
    t0 = time.time()
    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)
    for tid in range(1, tenant_count + 1):
        os.makedirs(os.path.join(staging_dir, f"t_{tid}"), exist_ok=True)

    # Table metadata: (table_name, tenant_col_index)
    tables_meta = [
        ("tenants", 0),
        ("products", 1),
        ("customers", 1),
        ("orders", 1),
        ("order_items", 2),
    ]

    for table, col_idx in tables_meta:
        gz_path = os.path.join(data_dir, f"{table}.csv.gz")
        print(f"  Streaming {table} from {gz_path} ...", flush=True)
        t_tbl = time.time()
        fps = {
            tid: open(os.path.join(staging_dir, f"t_{tid}", f"{table}.csv"), "w", encoding="utf-8", buffering=65536)
            for tid in range(1, tenant_count + 1)
        }

        with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
            header = gz.readline()  # Skip header
            line_cnt = 0
            for line in gz:
                line_cnt += 1
                # Fast column extraction
                start = 0
                for _ in range(col_idx):
                    start = line.find(",", start) + 1
                end = line.find(",", start)
                tid_str = line[start:] if end == -1 else line[start:end]
                try:
                    tid = int(tid_str.strip())
                    if 1 <= tid <= tenant_count:
                        fps[tid].write(line)
                except ValueError:
                    pass

        for fp in fps.values():
            fp.close()
        print(f"    Streamed {line_cnt:,} {table} rows in {time.time() - t_tbl:.2f}s")

    print(f"  Streaming & demuxing completed in {time.time() - t0:.2f}s")

    # 4. Parallel COPY, post_load.sql, and ANALYZE per tenant
    print(f"\n[4/5] Parallel COPY, index creation (post_load.sql), and ANALYZE ({max_workers} threads) ...")
    t0 = time.time()

    def load_tenant(tid: int):
        db_url = get_tenant_db_url(tid, profile)
        t_conn = psycopg2.connect(db_url)
        t_conn.autocommit = True
        t_cur = t_conn.cursor()

        tenant_folder = os.path.join(staging_dir, f"t_{tid}")
        for table, _ in tables_meta:
            csv_path = os.path.join(tenant_folder, f"{table}.csv")
            if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                with open(csv_path, "r", encoding="utf-8") as f:
                    t_cur.copy_expert(f"COPY {table} FROM STDIN WITH CSV", f)

        # Run post_load.sql (indexes and foreign keys) and ANALYZE
        t_cur.execute(post_load_sql)
        t_cur.close()
        t_conn.close()
        return tid

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        completed = 0
        for tid in executor.map(load_tenant, range(1, tenant_count + 1)):
            completed += 1
            if completed % 25 == 0 or completed == tenant_count:
                print(f"  Finished loading & indexing {completed}/{tenant_count} tenant DBs")

    # Clean up staging files
    shutil.rmtree(staging_dir, ignore_errors=True)
    print(f"  All tenant DBs loaded and indexed in {time.time() - t0:.2f}s")

    # 5. Populate tenant_registry & worker_state in Rollup DB
    print(f"\n[5/5] Registering tenants in Rollup DB ...")
    t0 = time.time()
    rollup_url = cfg["database"]["rollup_db_url"]
    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()

    key_prefix = cfg.get("auth", {}).get("key_prefix", "tenant-key-")
    for tid in range(1, tenant_count + 1):
        db_name = get_tenant_db_name(tid, profile)
        api_key = f"{key_prefix}{tid}"
        r_cur.execute("""
            INSERT INTO tenant_registry (tenant_id, database_name, api_key)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_id) 
            DO UPDATE SET database_name = EXCLUDED.database_name, api_key = EXCLUDED.api_key
        """, (tid, db_name, api_key))

        r_cur.execute("""
            INSERT INTO worker_state (tenant_id, cursor_xid, cursor_id, heartbeat, stalled)
            VALUES (%s, '0'::xid8, 0, clock_timestamp(), false)
            ON CONFLICT (tenant_id) DO NOTHING
        """, (tid,))

    r_cur.close()
    r_conn.close()
    print(f"  Registered {tenant_count} tenants in {time.time() - t0:.2f}s")

    total_time = time.time() - t_start
    print("=" * 70)
    print(f"SPLIT & LOAD COMPLETE (PROFILE: {profile.upper()}) in {total_time:.2f}s")
    print("=" * 70)
    return total_time

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split dataset per tenant into separate databases.")
    parser.add_argument("--profile", default="1m", choices=["1m", "10m"], help="Dataset profile to load")
    parser.add_argument("--tenants", type=int, default=100, help="Number of tenants")
    parser.add_argument("--workers", type=int, default=16, help="Parallel worker threads")
    args = parser.parse_args()

    split_and_load(profile=args.profile, tenant_count=args.tenants, max_workers=args.workers)
