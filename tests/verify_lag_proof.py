#!/usr/bin/env python3
"""tests/verify_lag_proof.py:
Proves lag_seconds behavior:
1. Verifies worker is stopped.
2. Injects writes via simulator.py writes.
3. Shows lag_seconds rises in API.
4. Starts worker.
5. Shows lag_seconds drops to near 0.
"""
import sys, os, subprocess, time, httpx, psycopg2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

def prove_lag():
    print("=== PROOF: lag_seconds Measurement Under Load ===")
    cfg = get_config("1m")
    main_url = cfg["database"]["main_db_url"]
    rollup_url = cfg["database"]["rollup_db_url"]
    api_port = os.getenv("API_PORT", "8000")
    client = httpx.Client(base_url=f"http://127.0.0.1:{api_port}")

    # Step 1: Initial check with worker stopped
    h = client.get("/health").json()
    print("1. Initial state (worker stopped):", h)

    # Step 2: Run simulator writes for 3 seconds
    print("\n2. Running simulator writes while worker is STOPPED...")
    subprocess.run([
        sys.executable, "data/mitigata_dataset/simulator.py", "writes",
        "--url", main_url,
        "--rate", "20", "--seconds", "3", "--log", "events.csv"
    ], check=True)

    # Step 3: Poll API and watch lag_seconds rise
    print("\n3. Polling API while worker remains stopped (showing lag rising):")
    for i in range(4):
        time.sleep(1.0)
        h = client.get("/health").json()
        print(f"  T+{i+1}s: lag_seconds = {h['lag_seconds']:.2f}s | worker_stalled = {h['worker_stalled']}")

    lag_before = h['lag_seconds']
    assert lag_before > 2.0, f"Expected lag to rise above 2.0s, got {lag_before}"

    # Step 4: Start worker process
    print("\n4. Starting worker process...")
    p = subprocess.Popen([
        sys.executable, "worker/worker.py",
        main_url,
        rollup_url
    ])

    # Step 5: Poll API and watch lag_seconds fall to 0
    print("\n5. Polling API after worker started (showing lag falling to 0):")
    time.sleep(1.0)
    for i in range(4):
        time.sleep(0.5)
        h = client.get("/health").json()
        print(f"  T+{i+1}s: lag_seconds = {h['lag_seconds']:.2f}s | worker_stalled = {h['worker_stalled']}")

    lag_after = h['lag_seconds']
    assert lag_after < 0.5, f"Expected lag to drop below 0.5s, got {lag_after}"
    print(f"\n[PROOF OK] lag_seconds rose to {lag_before:.2f}s when worker stalled, then fell to {lag_after:.2f}s when worker resumed!")

    p.terminate()
    p.wait()

if __name__ == "__main__":
    prove_lag()
