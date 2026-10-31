#!/usr/bin/env python3
"""tests/run_all.py:
Unified Trust & Measurement Test Suite for Multi-Tenant Aggregation Layer:
Runs all 11 test suites for a given profile (1m or 10m).
Exits non-zero on any failure.
"""
import sys, os, time, argparse, subprocess, httpx, psycopg2
from typing import Optional
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.test_isolation import run_isolation_tests
from tests.test_idempotency import run_idempotency_test
from tests.test_out_of_order import run_out_of_order_test
from tests.mutate import run_mutations
from tests.test_concurrent_multi_tenant import run_concurrent_multi_tenant_test
from tests.test_fairness import run_fairness_test
from tests.test_quarantine import run_quarantine_test
from tests.test_crash_recovery import run_crash_recovery_test
from tests.test_fail_closed import run_fail_closed_test
from tests.test_rebuild_equality import run_rebuild_equality_test
from tests.test_golden_parity import run_golden_check
from tests.integrity_tripwire import verify_integrity_tripwire
from tests.sync import wait_for_worker
from config import get_config

def reset_all_worker_state(rollup_url: str):
    conn = psycopg2.connect(rollup_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE worker_state SET stalled = false, last_error = NULL")
    cur.close()
    conn.close()

def wait_for_api_ready(api_url: Optional[str] = None, max_wait: float = 15.0):
    url = api_url or f"http://127.0.0.1:{os.getenv('API_PORT', '8000')}"
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                print("  FastAPI server is ready!")
                return True
        except Exception:
            time.sleep(0.5)
    raise TimeoutError("FastAPI server failed to start within timeout")

def run_all(profile: str):
    print("=" * 70)
    print(f"RUNNING ALL TRUST & INTEGRITY TESTS (PROFILE: {profile.upper()})")
    print("=" * 70)

    cfg = get_config(profile)
    rollup_url = cfg["database"]["rollup_db_url"]

    # 1. Reset worker states in rollup-db
    reset_all_worker_state(rollup_url)

    # 2. Restart API cleanly for the requested profile
    p_api = None
    try:
        subprocess.run(["powershell", "-Command", "Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*uvicorn*' } | Stop-Process -Force"], timeout=5)
    except Exception:
        pass
    time.sleep(1.0)

    api_port = os.getenv("API_PORT", "8000")
    api_url = f"http://127.0.0.1:{api_port}"
    print(f"Starting FastAPI server on port {api_port} (profile: {profile})...")
    p_api = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--port", str(api_port)], env=dict(os.environ, PROFILE=profile))
    wait_for_api_ready(api_url)

    # 3. Start unified runner worker daemon
    print("Starting background multi-tenant rollup worker...")
    p_worker = subprocess.Popen([sys.executable, "worker/worker.py", "--profile", profile])
    time.sleep(1.5)
    wait_for_worker(profile, timeout=15.0)

    def pause_worker():
        nonlocal p_worker
        if p_worker and p_worker.poll() is None:
            p_worker.terminate()
            p_worker.wait()
            p_worker = None

    def resume_worker():
        nonlocal p_worker
        reset_all_worker_state(rollup_url)
        if p_worker is None or p_worker.poll() is not None:
            p_worker = subprocess.Popen([sys.executable, "worker/worker.py", "--profile", profile])
            time.sleep(1.5)

    # All 11 suites as required
    suites = [
        ("1. Tenant Isolation", lambda: run_isolation_tests(profile), False),
        ("2. Idempotency per-tenant", lambda: run_idempotency_test(profile), False),
        ("3. Out-of-Order Commit per-tenant", lambda: run_out_of_order_test(profile), False),
        ("4. Single-Tenant Mutation & Fan-out", lambda: run_mutations(profile, tenant_id=1), False),
        ("5. Concurrent Multi-Tenant Updates (25 DBs)", lambda: run_concurrent_multi_tenant_test(profile, tenant_count=25), False),
        ("6. Fairness & Anti-Starvation", lambda: run_fairness_test(profile, flood_count=2000), False),
        ("7. Quarantine Handling", lambda: run_quarantine_test(profile), False),
        ("8. Crash Recovery per-tenant", lambda: run_crash_recovery_test(profile), True),  # Manages its own worker
        ("9. Per-Tenant Fail Closed & Stall Isolation", lambda: run_fail_closed_test(profile), False),
        ("10. Rebuild Equality under Load per-tenant", lambda: run_rebuild_equality_test(profile), True),  # Manages its own worker
        ("11. Golden Ground-Truth Parity vs Tenant DBs", lambda: (wait_for_worker(profile) is None and run_golden_check(profile)), False),
    ]

    results = []
    failed = False

    try:
        for name, fn, needs_standalone_worker in suites:
            print(f"\n>>> Running Suite: {name} ...")
            t0 = time.time()
            reset_all_worker_state(rollup_url)
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
        # Cleanup background processes
        if p_worker and p_worker.poll() is None:
            p_worker.terminate()
            p_worker.wait()
        if p_api and p_api.poll() is None:
            p_api.terminate()
            p_api.wait()

    # Strict dataset integrity tripwire check
    print("\n>>> Running Final Data Integrity Tripwire Check...")
    tripwire_ok = verify_integrity_tripwire(profile, fail_fast=False)
    if not tripwire_ok:
        failed = True
        results.append(("12. Dataset Integrity Tripwire", "FAIL", 0.0))
    else:
        results.append(("12. Dataset Integrity Tripwire", "PASS", 0.0))

    print("\n" + "=" * 70)
    print(f"TEST SUITE SUMMARY ({profile.upper()}):")
    for name, status, elapsed in results:
        status_str = f"PASS ({elapsed:.2f}s)" if status == "PASS" else f"FAIL ({elapsed:.2f}s)"
        print(f"  {name:<45}:     {status_str}")
    print("=" * 70)

    if failed:
        print("FAILURE: One or more test suites failed.")
        sys.exit(1)
    else:
        print("SUCCESS: All 11 test suites passed with 100% ground-truth parity!")
        sys.exit(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified test harness for CyberForge aggregation layer.")
    parser.add_argument("--profile", default="1m", choices=["1m", "10m"], help="Profile to test")
    args = parser.parse_args()
    run_all(args.profile)
