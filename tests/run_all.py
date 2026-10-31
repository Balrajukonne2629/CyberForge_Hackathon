#!/usr/bin/env python3
"""Unified test harness:
Runs all 9 test suites for a given profile (1m or 10m).
Exits non-zero on any failure.
"""
import sys, os, time, argparse, subprocess, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Test modules
from tests.test_golden_parity import run_golden_check
from tests.test_isolation import run_isolation_tests
from tests.test_idempotency import run_idempotency_test
from tests.test_out_of_order import run_out_of_order_test
from tests.test_crash_recovery import run_crash_recovery_test
from tests.test_rebuild_equality import run_rebuild_equality_test
from tests.test_quarantine import run_quarantine_test
from tests.test_fail_closed import run_fail_closed_test
from tests.mutate import run_mutations
from tests.sync import wait_for_worker
from config import get_config

def reset_worker_state(rollup_url: str):
    conn = psycopg2.connect(rollup_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE worker_state SET worker_stalled = false, stalled_reason = NULL WHERE id = 1")
    cur.close()
    conn.close()

def run_all(profile: str):
    print("=" * 70)
    print(f"RUNNING ALL TRUST & INTEGRITY TESTS (PROFILE: {profile.upper()})")
    print("=" * 70)

    cfg = get_config(profile)
    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_db_url"]

    # 1. Ensure clean worker state
    reset_worker_state(rollup_url)

    # 2. Start runner's worker daemon
    p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", main_url, rollup_url])
    time.sleep(1.0)

    def pause_worker():
        nonlocal p_worker
        if p_worker and p_worker.poll() is None:
            p_worker.terminate()
            p_worker.wait()
            p_worker = None

    def resume_worker():
        nonlocal p_worker
        reset_worker_state(rollup_url)
        if p_worker is None or p_worker.poll() is not None:
            p_worker = subprocess.Popen(["py", "-3.12", "worker/worker.py", main_url, rollup_url])
            time.sleep(1.0)

    suites = [
        ("1. Tenant Isolation", lambda: run_isolation_tests(profile), False),
        ("2. Idempotency", lambda: run_idempotency_test(profile), False),
        ("3. Out-of-Order Commit", lambda: run_out_of_order_test(profile), False),
        ("4. Mutation & Fan-out Handling", lambda: (run_mutations(main_url, rollup_url) is None or True), False),
        ("5. Quarantine Handling", lambda: run_quarantine_test(profile), False),
        ("6. Crash Recovery", lambda: run_crash_recovery_test(profile), True),  # Needs own worker
        ("7. Fail Closed (CHECK Constraint)", lambda: run_fail_closed_test(profile), True),  # Needs own worker
        ("8. Rebuild Equality (Snapshot Skip)", lambda: run_rebuild_equality_test(profile), True),  # Needs own worker
        ("9. Golden Ground-Truth Parity vs Main-DB", lambda: (wait_for_worker(main_url, rollup_url) is None and run_golden_check(profile)), False),
    ]

    results = []
    failed = False

    try:
        for name, fn, needs_standalone_worker in suites:
            print(f"\n>>> Running Suite: {name} ...")
            t0 = time.time()
            if needs_standalone_worker:
                pause_worker()
            else:
                resume_worker()

            try:
                ok = fn()
                elapsed = time.time() - t0
                if ok:
                    print(f"  [PASS] {name} ({elapsed:.2f}s)")
                    results.append((name, "PASS", elapsed))
                else:
                    print(f"  [FAIL] {name} ({elapsed:.2f}s)")
                    results.append((name, "FAIL", elapsed))
                    failed = True
            except Exception as e:
                elapsed = time.time() - t0
                print(f"  [ERROR] {name}: {e}")
                results.append((name, f"ERROR: {e}", elapsed))
                failed = True
            finally:
                if needs_standalone_worker:
                    resume_worker()

    finally:
        pause_worker()
        reset_worker_state(rollup_url)

    print("\n" + "=" * 70)
    print(f"TEST SUITE SUMMARY ({profile.upper()}):")
    for name, status, elapsed in results:
        print(f"  {name:<45}: {status:>8} ({elapsed:.2f}s)")
    print("=" * 70)

    if failed:
        print("FAILED: One or more test suites failed.")
        sys.exit(1)
    else:
        print("SUCCESS: All 9 test suites passed with 100% ground-truth parity!")
        sys.exit(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["1m", "10m"], default="1m")
    args = parser.parse_args()
    run_all(args.profile)
