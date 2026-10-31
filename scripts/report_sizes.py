#!/usr/bin/env python3
"""Report table sizes and row counts for rollup_1m and rollup_10m."""
import psycopg2, os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def report_sizes():
    rollup_host = os.getenv("ROLLUP_DB_HOST", "127.0.0.1")
    rollup_port = int(os.getenv("ROLLUP_DB_PORT", 5433))
    rollup_user = os.getenv("ROLLUP_DB_USER", "postgres")
    rollup_pwd = os.getenv("ROLLUP_DB_PASSWORD", "postgres")

    for db in ['rollup_1m', 'rollup_10m']:
        conn = psycopg2.connect(f'postgresql://{rollup_user}:{rollup_pwd}@{rollup_host}:{rollup_port}/{db}')
        cur = conn.cursor()
        print(f"=== Database: {db} ===")
        for tbl in ['rollup_orders_hour', 'rollup_product_day', 'worker_state', 'quarantine']:
            cur.execute(f"SELECT count(*) FROM {tbl}")
            cnt = cur.fetchone()[0]
            cur.execute(f"SELECT pg_size_pretty(pg_total_relation_size('{tbl}'))")
            sz = cur.fetchone()[0]
            print(f"  {tbl:<22}: {cnt:>10,} rows | {sz:>8}")
        cur.close()
        conn.close()

if __name__ == "__main__":
    report_sizes()
