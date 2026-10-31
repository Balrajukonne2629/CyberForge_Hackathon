#!/usr/bin/env python3
"""bench/multi_sim.py:
Multi-Tenant Live Write Simulator + OLTP Latency Probe.
Wraps the behavior of simulator.py across 100 tenant databases with realistic tenant skew.

Supported modes and arguments (identical interface to simulator.py):
  python bench/multi_sim.py writes --rate 20 --seconds 30 --log events.csv --profile 1m
      Inserts new orders (+items) and advances/cancels existing orders across tenant DBs.
      Logs (kind, tenant_id, order_id, status, committed_at) to CSV.
  python bench/multi_sim.py oltp --seconds 30 --profile 1m
      Runs point reads and small updates across tenant DBs and prints p50/p95/p99 ms.
"""
import sys, os, time, csv, random, argparse
from typing import Dict, List, Tuple
import numpy as np
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_tenant_db_url, get_all_tenant_ids

NEXT = {
    "created": "approved",
    "approved": "invoiced",
    "invoiced": "processing",
    "processing": "shipped",
    "shipped": "delivered"
}

def get_tenant_weights(tenants: List[int]) -> List[float]:
    # Zipfian / power-law distribution: weight = 1.0 / (rank ^ 0.8)
    return [1.0 / (i ** 0.8) for i in range(1, len(tenants) + 1)]

def run_multi_writes(args):
    print(f"=== Multi-Tenant Live Write Simulator (Profile: {args.profile}) ===")
    print(f"Target: {args.seconds}s @ {args.rate} writes/s | Insert share: {args.insert_share} | Log: {args.log}")

    rng = random.Random(args.seed)
    all_tids = get_all_tenant_ids(args.tenants)
    weights = get_tenant_weights(all_tids)

    # Establish connections and cache tenant metadata
    conns: Dict[int, psycopg2.extensions.connection] = {}
    curs: Dict[int, psycopg2.extensions.cursor] = {}
    custs: Dict[int, List[int]] = {}
    prods: Dict[int, List[Tuple[int, float]]] = {}
    pools: Dict[int, List[Tuple[int, int, str]]] = {}
    max_ids: Dict[int, List[int]] = {}  # [max_order_id, max_item_id]

    print(f"Connecting to {len(all_tids)} tenant databases and caching metadata...")
    t0_init = time.time()
    for tid in all_tids:
        url = get_tenant_db_url(tid, args.profile)
        c = psycopg2.connect(url)
        c.autocommit = False
        cur = c.cursor()
        conns[tid] = c
        curs[tid] = cur

        # Fetch max order_id and item_id
        cur.execute("SELECT coalesce(max(order_id), 0), coalesce((SELECT max(item_id) FROM order_items), 0) FROM orders")
        moid, miid = cur.fetchone()
        max_ids[tid] = [moid, miid]

        # Sample customers
        cur.execute("SELECT customer_id FROM customers LIMIT 200")
        c_rows = [r[0] for r in cur.fetchall()]
        custs[tid] = c_rows if c_rows else [1]

        # Sample products
        cur.execute("SELECT product_id, price FROM products LIMIT 200")
        p_rows = [(r[0], float(r[1])) for r in cur.fetchall()]
        prods[tid] = p_rows if p_rows else [(1, 10.0)]

        # Sample open orders that can change
        cur.execute("""
            SELECT order_id, tenant_id, status 
            FROM orders 
            WHERE status IN ('created','approved','invoiced','processing','shipped')
            ORDER BY order_id DESC LIMIT 100
        """)
        pools[tid] = list(cur.fetchall())
        c.commit()

    print(f"Initialized simulator metadata in {time.time() - t0_init:.2f}s")

    with open(args.log, "w", newline="", encoding="utf-8") as f:
        lg = csv.writer(f)
        lg.writerow(["kind", "tenant_id", "order_id", "status", "committed_at"])

        end_time = time.time() + args.seconds
        total_writes = 0

        while time.time() < end_time:
            t_op_start = time.time()

            # Choose tenant based on skewed distribution
            tid = rng.choices(all_tids, weights=weights)[0]
            cur = curs[tid]
            conn = conns[tid]
            pool = pools[tid]

            if rng.random() < args.insert_share or not pool:
                # Insert order + item
                cu = rng.choice(custs[tid])
                pid, pr = rng.choice(prods[tid])
                q = rng.choice([1, 1, 2, 3])
                max_ids[tid][0] += 1
                max_ids[tid][1] += 1
                oid = max_ids[tid][0]
                iid = max_ids[tid][1]

                cur.execute(
                    "INSERT INTO orders (order_id, tenant_id, customer_id, status, total, created_at, updated_at) "
                    "VALUES (%s, %s, %s, 'created', %s, clock_timestamp(), clock_timestamp())",
                    (oid, tid, cu, pr * q)
                )
                cur.execute(
                    "INSERT INTO order_items (item_id, order_id, tenant_id, product_id, qty, price) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (iid, oid, tid, pid, q, pr)
                )
                conn.commit()
                committed_at = time.time()
                pool.append((oid, tid, "created"))
                lg.writerow(["insert", tid, oid, "created", committed_at])
            else:
                # Advance or cancel open order
                idx = rng.randrange(len(pool))
                oid, t_orig, st = pool[idx]
                new_status = "canceled" if rng.random() < 0.1 else NEXT[st]

                cur.execute(
                    "UPDATE orders SET status = %s, updated_at = clock_timestamp() WHERE order_id = %s",
                    (new_status, oid)
                )
                conn.commit()
                committed_at = time.time()
                lg.writerow(["update", tid, oid, new_status, committed_at])

                if new_status in ("canceled", "delivered"):
                    pool[idx] = pool[-1]
                    pool.pop()
                else:
                    pool[idx] = (oid, tid, new_status)

            total_writes += 1
            f.flush()

            # Rate throttling
            elapsed = time.time() - t_op_start
            sleep_time = max(0.0, (1.0 / args.rate) - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

    # Cleanup connections
    for c in conns.values():
        c.close()

    print(f"Completed {total_writes} writes in {args.seconds}s -> {args.log}")

def run_multi_oltp(args):
    print(f"=== Multi-Tenant OLTP Latency Probe (Profile: {args.profile}) ===")
    print(f"Target duration: {args.seconds}s | Random point reads & updates across tenant DBs")

    rng = random.Random(args.seed)
    all_tids = get_all_tenant_ids(args.tenants)
    weights = get_tenant_weights(all_tids)

    conns = {}
    curs = {}
    max_orders = {}

    for tid in all_tids:
        url = get_tenant_db_url(tid, args.profile)
        c = psycopg2.connect(url)
        c.autocommit = True
        conns[tid] = c
        curs[tid] = c.cursor()
        curs[tid].execute("SELECT coalesce(max(order_id), 1) FROM orders")
        max_orders[tid] = curs[tid].fetchone()[0]

    latencies_ms = []
    end_time = time.time() + args.seconds

    while time.time() < end_time:
        tid = rng.choices(all_tids, weights=weights)[0]
        cur = curs[tid]
        mx = max_orders[tid]
        order_id = rng.randint(1, mx)

        t0 = time.perf_counter()
        if rng.random() < 0.8:
            # Point read: order joined with order_items
            cur.execute("""
                SELECT o.order_id, o.status, o.total, i.product_id, i.qty, i.price
                FROM orders o
                JOIN order_items i ON o.order_id = i.order_id
                WHERE o.order_id = %s
            """, (order_id,))
            cur.fetchall()
        else:
            # Small update: touch updated_at
            cur.execute("UPDATE orders SET updated_at = clock_timestamp() WHERE order_id = %s", (order_id,))

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(elapsed_ms)

    for c in conns.values():
        c.close()

    p50, p95, p99 = np.percentile(latencies_ms, [50, 95, 99])
    print(f"{len(latencies_ms)} ops | p50 {p50:.2f} ms | p95 {p95:.2f} ms | p99 {p99:.2f} ms")
    return {"ops": len(latencies_ms), "p50": p50, "p95": p95, "p99": p99}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-tenant live write simulator & OLTP probe.")
    parser.add_argument("mode", choices=["writes", "oltp"], help="Mode: writes or oltp")
    parser.add_argument("--profile", default="1m", choices=["1m", "10m"], help="Profile")
    parser.add_argument("--tenants", type=int, default=100, help="Number of tenants")
    parser.add_argument("--seconds", type=int, default=30, help="Duration in seconds")
    parser.add_argument("--rate", type=float, default=20.0, help="Writes per second")
    parser.add_argument("--insert-share", type=float, default=0.6, help="Fraction of writes that are inserts")
    parser.add_argument("--log", default="events.csv", help="Log CSV filename")
    parser.add_argument("--seed", type=int, default=1, help="Random seed")
    args = parser.parse_args()

    if args.mode == "writes":
        run_multi_writes(args)
    else:
        run_multi_oltp(args)
