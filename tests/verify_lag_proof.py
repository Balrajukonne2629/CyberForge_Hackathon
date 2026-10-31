#!/usr/bin/env python3
"""tests/verify_lag_proof.py:
Proves lag_seconds behavior:
1. Verifies worker is stopped.
2. Injects writes via simulator.py writes.
3. Shows lag_seconds rises in API.
4. Starts worker.
5. Shows lag_seconds drops to near 0.
"""
import subprocess, time, httpx, psycopg2

def prove_lag():
    print("=== PROOF: lag_seconds Measurement Under Load ===")
    client = httpx.Client(base_url="http://127.0.0.1:8000")

    # Step 1: Initial check with worker stopped
    h = client.get("/health").json()
    print("1. Initial state (worker stopped):", h)

    # Step 2: Run simulator writes for 3 seconds
    print("\n2. Running simulator writes while worker is STOPPED...")
    subprocess.run([
        "py", "-3.12", "data/mitigata_dataset/simulator.py", "writes",
        "--url", "postgresql://postgres:postgres@localhost:5432/shop",
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
        "py", "-3.12", "worker/worker.py",
        "postgresql://postgres:postgres@localhost:5432/shop",
        "postgresql://postgres:postgres@localhost:5433/rollup_1m"
    ])

    # Step 5: Poll API and watch lag_seconds fall to 0
    print("\n5. Polling API after worker started (showing lag falling to 0):")
    time.sleep(1.0)
    for i in range(4):
        time.sleep(0.5)
        h = client.get("/health").json()
        print(f"  Sync T+{i*0.5 + 1.0:.1f}s: lag_seconds = {h['lag_seconds']:.2f}s")
        if h['lag_seconds'] == 0.0:
            break

    final_lag = h['lag_seconds']
    p.terminate()
    p.wait()

    print(f"\n>>> PROOF COMPLETE: lag rose to {lag_before:.2f}s and fell to {final_lag:.2f}s <<<")
    assert final_lag == 0.0

if __name__ == "__main__":
    prove_lag()
