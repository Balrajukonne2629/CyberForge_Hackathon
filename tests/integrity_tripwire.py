#!/usr/bin/env python3
"""tests/integrity_tripwire.py:
Strict Data Integrity Tripwire:
Sums orders and order_items across all 100 tenant databases for a given profile.
Compares against the exact CSV source line counts.
Fails loudly and non-zero on any discrepancy.
"""
import sys, os, time, psycopg2
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_tenant_db_url

EXPECTED_COUNTS = {
    "1m": {
        "orders": 1_000_000,
        "order_items": 1_141_449,
    },
    "10m": {
        "orders": 10_000_000,
        "order_items": 11_416_141,
    }
}

def check_tenant(tid: int, profile: str):
    url = get_tenant_db_url(tid, profile)
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM orders")
    orders = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM order_items")
    items = cur.fetchone()[0]
    cur.close()
    conn.close()
    return orders, items

def verify_integrity_tripwire(profile: str = "1m", fail_fast: bool = True) -> bool:
    expected = EXPECTED_COUNTS.get(profile.lower())
    if not expected:
        raise ValueError(f"Unknown profile: {profile}")

    print(f"\n[TRIPWIRE] Verifying dataset integrity across 100 tenant DBs (profile: {profile})...")
    t0 = time.time()
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda tid: check_tenant(tid, profile), range(1, 101)))

    total_orders = sum(r[0] for r in results)
    total_items = sum(r[1] for r in results)
    elapsed = time.time() - t0

    exp_orders = expected["orders"]
    exp_items = expected["order_items"]

    if total_orders != exp_orders or total_items != exp_items:
        msg = (
            f"\n"
            f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            f"[TRIPWIRE VIOLATION] DATA INTEGRITY MISMATCH DETECTED!\n"
            f"  Orders:      actual {total_orders:,} vs expected {exp_orders:,} (diff: {total_orders - exp_orders:+d})\n"
            f"  Order Items: actual {total_items:,} vs expected {exp_items:,} (diff: {total_items - exp_items:+d})\n"
            f"  Elapsed:     {elapsed:.2f}s\n"
            f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        )
        print(msg, file=sys.stderr)
        if fail_fast:
            raise AssertionError(msg)
        return False

    print(f"  [TRIPWIRE OK] Exact match in {elapsed:.2f}s: Orders = {total_orders:,} | Order Items = {total_items:,}")
    return True

if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "1m"
    try:
        verify_integrity_tripwire(prof, fail_fast=True)
    except AssertionError:
        sys.exit(1)
