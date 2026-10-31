#!/usr/bin/env python3
"""scripts/setup.py:
One-command setup for the Multi-Tenant Aggregation Layer.
Can be run on a clean machine by a judge, evaluator, or teammate:
  python scripts/setup.py --profile 1m
  python scripts/setup.py --profile 10m

Orchestrates:
1. Environment pre-flight checks:
   - Docker daemon connectivity check.
   - Available disk space estimation and verification.
   - WSL2 / system memory check.
   - Dataset archive auto-extraction (DATASET_ZIP or data/mitigata_dataset.zip).
2. Docker compose service initialization (main-db, rollup-db).
3. Schema creation on rollup-db.
4. Multi-tenant database split, template cloning, CSV COPY, post_load indexes, ANALYZE.
5. Outbox & statement-level triggers installation across all 100 tenant DBs.
6. Full initial parallel rebuild of rollup-db.
7. Split completeness guard verification against raw source CSV lines.
8. API / Worker healthcheck smoke test.
"""
import sys, os, time, subprocess, argparse, zipfile, shutil, platform
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config, get_admin_db_url
from scripts.split_by_tenant import split_and_load, verify_split_completeness
from scripts.install_triggers import install_triggers
from scripts.rebuild import rebuild_all

# Pre-flight resource requirements
PROFILE_REQUIREMENTS = {
    "1m": {
        "min_disk_gb": 1.5,
        "recommended_disk_gb": 3.0,
        "breakdown": "550 MB tenant DBs (100 DBs) + 50 MB staging + 100 MB rollup + 300 MB logs/WAL"
    },
    "10m": {
        "min_disk_gb": 12.0,
        "recommended_disk_gb": 20.0,
        "breakdown": "5.5 GB tenant DBs (100 DBs) + 450 MB staging + 1.2 GB rollup + 4-5 GB logs/WAL"
    }
}

def check_docker() -> bool:
    print("[Pre-flight 1/4] Checking Docker daemon status ...")
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            raise RuntimeError(res.stderr or res.stdout)
        print("  Docker daemon is running and accessible.")
        return True
    except Exception as e:
        print("\n" + "!" * 70, file=sys.stderr)
        print("[SETUP ERROR] Docker is not running or not accessible!", file=sys.stderr)
        print("Fix:", file=sys.stderr)
        print("  1. Launch Docker Desktop on Windows.", file=sys.stderr)
        print("  2. Ensure 'Use the WSL 2 based engine' is enabled in Docker Settings -> General.", file=sys.stderr)
        print("  3. Verify 'docker ps' works in PowerShell before retrying.", file=sys.stderr)
        print("!" * 70 + "\n", file=sys.stderr)
        sys.exit(1)

def check_disk_space(profile: str):
    print(f"[Pre-flight 2/4] Checking free disk space for profile '{profile}' ...")
    req = PROFILE_REQUIREMENTS.get(profile, PROFILE_REQUIREMENTS["1m"])
    try:
        total, used, free = shutil.disk_usage(os.path.abspath("."))
        free_gb = free / (1024 ** 3)
        print(f"  Available disk space: {free_gb:.1f} GB (Profile {profile.upper()} requires ~{req['min_disk_gb']} GB min, recommended {req['recommended_disk_gb']} GB)")
        print(f"  Storage breakdown: {req['breakdown']}")
        if free_gb < req['min_disk_gb']:
            print("\n" + "!" * 70, file=sys.stderr)
            print(f"[SETUP ERROR] Insufficient disk space for profile '{profile}'!", file=sys.stderr)
            print(f"  Available: {free_gb:.2f} GB | Required: {req['min_disk_gb']} GB", file=sys.stderr)
            print("Fix: Free up disk space on the repository drive before proceeding.", file=sys.stderr)
            print("!" * 70 + "\n", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"  Warning: Could not determine disk space ({e}), continuing...")

def check_wsl_memory():
    print("[Pre-flight 3/4] Checking system / WSL2 memory allocation ...")
    try:
        wslconfig_path = os.path.expanduser("~/.wslconfig")
        if os.path.exists(wslconfig_path):
            with open(wslconfig_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                print(f"  Found ~/.wslconfig configuration:\n    " + content.strip().replace("\n", "\n    "))
        else:
            print("  Note: ~/.wslconfig not found. For optimal 10M performance on 16GB RAM machines,")
            print("        create %USERPROFILE%\\.wslconfig with: [wsl2]\\nmemory=12GB\\nprocessors=10")
    except Exception:
        pass

def check_and_extract_dataset(profile: str):
    print(f"[Pre-flight 4/4] Verifying dataset files for profile '{profile}' ...")
    data_dir = "data/mitigata_dataset/small" if profile == "1m" else "data/mitigata_dataset/main"
    schema_path = "data/mitigata_dataset/schema.sql"

    if os.path.exists(data_dir) and os.path.exists(schema_path):
        print(f"  Dataset directory verified: {data_dir}")
        return

    # Check for zip archive
    zip_env = os.getenv("DATASET_ZIP")
    zip_candidates = [
        zip_env,
        "data/mitigata_dataset.zip",
        "mitigata_dataset.zip",
        os.path.join("data", f"mitigata_dataset_{profile}.zip")
    ]
    found_zip = None
    for z in zip_candidates:
        if z and os.path.isfile(z):
            found_zip = z
            break

    if found_zip:
        print(f"  Found dataset archive: {found_zip}. Extracting into data/ ...")
        os.makedirs("data", exist_ok=True)
        with zipfile.ZipFile(found_zip, "r") as zf:
            zf.extractall("data")
        print("  Extraction complete.")
        if os.path.exists(data_dir):
            return

    # Archive not found
    print("\n" + "!" * 70, file=sys.stderr)
    print(f"[SETUP ERROR] Mitigata dataset not found for profile '{profile}'!", file=sys.stderr)
    print(f"Expected folder: {os.path.abspath(data_dir)}", file=sys.stderr)
    print("Fix:", file=sys.stderr)
    print("  1. Download or copy 'mitigata_dataset.zip'.", file=sys.stderr)
    print("  2. Place it at 'data/mitigata_dataset.zip' OR set environment variable:", file=sys.stderr)
    print("     $env:DATASET_ZIP = 'C:\\path\\to\\mitigata_dataset.zip'", file=sys.stderr)
    print("  3. Alternatively, extract it directly so 'data/mitigata_dataset/' contains 'schema.sql', 'small/', and 'main/'.", file=sys.stderr)
    print("!" * 70 + "\n", file=sys.stderr)
    sys.exit(1)

def wait_for_db(url: str, label: str, max_retries: int = 30):
    print(f"Waiting for {label} to accept connections ({url}) ...")
    for i in range(max_retries):
        try:
            c = psycopg2.connect(url)
            c.close()
            print(f"  {label} is ready!")
            return
        except Exception:
            time.sleep(1.0)
    raise TimeoutError(f"Could not connect to {label} at {url} after {max_retries}s")

def smoke_test_api(profile: str):
    print("\n[Smoke Test] Verifying API and cluster health ...")
    api_port = os.getenv("API_PORT", "8000")
    api_url = f"http://127.0.0.1:{api_port}"
    p_api = None
    try:
        import httpx
        # Check if already running
        try:
            r = httpx.get(f"{api_url}/health", timeout=1.0)
            if r.status_code == 200:
                print(f"  API is already running: {r.json()}")
                return True
        except Exception:
            pass

        # Start API briefly
        p_api = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--port", str(api_port)], env=dict(os.environ, PROFILE=profile))
        for _ in range(15):
            time.sleep(1.0)
            try:
                r = httpx.get(f"{api_url}/health", timeout=1.0)
                if r.status_code == 200:
                    data = r.json()
                    print(f"  [SMOKE TEST PASS] API /health returned status={data.get('status')} | total_tenants={data.get('total_tenants')}")
                    return True
            except Exception:
                pass
        raise RuntimeError("API failed to respond to /health within 15 seconds")
    finally:
        if p_api:
            p_api.terminate()
            p_api.wait()

def setup_environment(profile: str = "1m", workers: int = 16):
    t_start = time.time()
    print("=" * 70)
    print(f"CYBERFORGE ONE-COMMAND SETUP (PROFILE: {profile.upper()})")
    print("=" * 70)

    # 1. Pre-flight checks
    check_docker()
    check_disk_space(profile)
    check_wsl_memory()
    check_and_extract_dataset(profile)

    cfg = get_config(profile)
    main_admin_url = get_admin_db_url()
    rollup_url = cfg["database"]["rollup_db_url"]

    # 2. Start Docker database containers
    print("\n[Step 1/5] Ensuring Docker database containers and volumes are running ...")
    compose_cmd = ["docker", "compose", "up", "-d", "main-db", "rollup-db"]
    res = subprocess.run(compose_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[SETUP ERROR] Failed to start Docker containers:\n{res.stderr}", file=sys.stderr)
        sys.exit(1)

    wait_for_db(main_admin_url, "Main PostgreSQL Server")
    wait_for_db(rollup_url, "Rollup Database Server")

    # 3. Initialize Rollup DB Schema
    print("\n[Step 2/5] Initializing Rollup DB schema ...")
    r_conn = psycopg2.connect(rollup_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()
    schema_path = "db/rollup/schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        r_cur.execute(f.read())
    r_cur.close()
    r_conn.close()
    print("  Rollup DB schema initialized.")

    # 4. Split & Load per-tenant databases (runs split completeness guard automatically)
    print(f"\n[Step 3/5] Splitting CSVs and loading 100 tenant databases ({profile}) ...")
    split_and_load(profile=profile, tenant_count=100, max_workers=workers)

    # 5. Install triggers across all tenant databases
    print(f"\n[Step 4/5] Installing outbox & triggers on 100 tenant databases ...")
    install_triggers(profile=profile, tenant_count=100, max_workers=workers)

    # 6. Full Rebuild into Rollup DB
    print(f"\n[Step 5/5] Performing initial parallel rebuild into Rollup DB ...")
    rebuild_all(profile=profile, tenant_count=100, max_workers=workers)

    # 7. Run split completeness verification guard
    print(f"\n[Verification] Running split completeness guard ...")
    verify_split_completeness(profile=profile, tenant_count=100, max_workers=workers)

    # 8. Smoke test API
    smoke_test_api(profile=profile)

    total_time = time.time() - t_start
    print("=" * 70)
    print(f"SETUP COMPLETE FOR PROFILE {profile.upper()} IN {total_time:.2f}s!")
    print("=" * 70)
    print("Next commands to run:")
    print(f"  1. Start worker:  python worker/worker.py --profile {profile}")
    print(f"  2. Start API:     python -m uvicorn api.main:app --port 8000")
    print(f"  3. Run tests:     python tests/run_all.py --profile {profile}")
    print(f"  4. Tripwire:      python tests/integrity_tripwire.py {profile}")
    print(f"  5. Benchmark:     python bench/bench.py --profile {profile}")
    print("=" * 70)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-command setup for CyberForge aggregation layer.")
    parser.add_argument("--profile", default="1m", choices=["1m", "10m"], help="Profile to set up")
    parser.add_argument("--workers", type=int, default=16, help="Parallel worker threads")
    args = parser.parse_args()

    setup_environment(profile=args.profile, workers=args.workers)
