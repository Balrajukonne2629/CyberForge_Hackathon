#!/usr/bin/env python3
"""Report table sizes and row counts for rollup_1m and rollup_10m."""
import psycopg2

def report_sizes():
    for db in ['rollup_1m', 'rollup_10m']:
        conn = psycopg2.connect(f'postgresql://postgres:postgres@localhost:5433/{db}')
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
