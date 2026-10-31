#!/usr/bin/env python3
"""scripts/install_triggers.py:
Applies outbox table and statement-level triggers to every tenant DB.
Reuses trigger definition logic from generate_triggers.py.
"""
import sys, os, time, argparse
from concurrent.futures import ThreadPoolExecutor
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_name, get_tenant_db_url
from scripts.generate_triggers import generate_sql

def install_triggers(profile: str = "1m", tenant_count: int = 100, max_workers: int = 16):
    cfg = get_config(profile)
    t0 = time.time()
    print("=" * 70)
    print(f"INSTALLING TRIGGERS & OUTBOX ON ALL TENANT DBS (PROFILE: {profile.upper()})")
    print(f"Tenants: {tenant_count} | Workers: {max_workers}")
    print("=" * 70)

    # 1. Generate SQL from config.yaml
    sql = generate_sql()
    print("Generated statement triggers and outbox schema.")

    # 2. Parallel execution across all tenant DBs
    def apply_to_tenant(tid: int):
        db_url = get_tenant_db_url(tid, profile)
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql)
        # Verify outbox table exists
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'outbox'")
        exists = cur.fetchone()[0] == 1
        cur.close()
        conn.close()
        if not exists:
            raise RuntimeError(f"Outbox table failed to create in tenant {tid}")
        return tid

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        completed = 0
        for tid in executor.map(apply_to_tenant, range(1, tenant_count + 1)):
            completed += 1
            if completed % 25 == 0 or completed == tenant_count:
                print(f"  Applied triggers to {completed}/{tenant_count} tenant DBs")

    elapsed = time.time() - t0
    print("=" * 70)
    print(f"TRIGGERS INSTALLED SUCCESSFULLY on {tenant_count} DBs in {elapsed:.2f}s")
    print("=" * 70)
    return elapsed

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install outbox and triggers on all tenant databases.")
    parser.add_argument("--profile", default="1m", choices=["1m", "10m"], help="Dataset profile")
    parser.add_argument("--tenants", type=int, default=100, help="Number of tenants")
    parser.add_argument("--workers", type=int, default=16, help="Parallel worker threads")
    args = parser.parse_args()

    install_triggers(profile=args.profile, tenant_count=args.tenants, max_workers=args.workers)
