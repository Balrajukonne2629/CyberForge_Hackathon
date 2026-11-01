> STALE: second-laptop run was not done. Current suite is 15 tests; see README.

# Teammate Setup Guide (Vignesh)

This guide provides exact copy-paste PowerShell instructions to clone, configure, and run CyberForge on a clean Windows 11 laptop (Lenovo i7-13620H, 16 GB RAM, Docker Desktop, WSL2).

---

## 1. Prerequisites Checklist

Before running any commands:
1. **Docker Desktop with WSL2 Engine**: Installed and running.
   - In Docker Settings -> General, ensure **"Use the WSL 2 based engine"** is checked.
2. **Python**: Python 3.10, 3.11, or 3.12 (Python 3.12 recommended). Ensure `python --version` works in PowerShell.
3. **Git**: Installed and in system PATH (`git --version`).
4. **Free Disk Space**: 20+ GB free on the SSD drive for the 10M profile (1M requires ~3 GB).
5. **Power Plan**: Laptop plugged into AC power and Windows Power Mode set to **"Best performance"**.
6. **WSL2 Memory Configuration (.wslconfig)**:
   - Press `Win + R`, enter `%USERPROFILE%`, and check if `.wslconfig` exists.
   - If not present (or to configure 12 GB RAM allocation for WSL2), create/edit `%USERPROFILE%\.wslconfig`:
     ```ini
     [wsl2]
     memory=12GB
     processors=10
     swap=4GB
     ```
   - Restart WSL2 in PowerShell: `wsl --shutdown`

---

## 2. Step-by-Step Setup Instructions

Open **PowerShell** (Run as Administrator or standard terminal):

### Step 1: Clone Repository
```powershell
git clone https://github.com/Balrajukonne2629/CyberForge_Hackathon.git cyberforge
cd cyberforge
```

### Step 2: Create Virtual Environment & Install Dependencies
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Step 3: Place Dataset Archive
Place `mitigata_dataset.zip` into the `data/` directory:
```powershell
# Create data folder if not present
New-Item -ItemType Directory -Force -Path data

# Copy your mitigata_dataset.zip into data\mitigata_dataset.zip
# Alternatively, set the environment variable:
# $env:DATASET_ZIP = "C:\Users\<user>\Downloads\mitigata_dataset.zip"
```
Verify the expected SHA-256 for the 5 raw CSV.GZ files:
- **1M Small Profile**:
  - `small/orders.csv.gz`: `d62a716a1c8162edfd5c3fb8b8aa121cfcc387dfe6c981713a41c0d27eb901ed` (26,864,848 bytes)
  - `small/order_items.csv.gz`: `4d39e9f823d73cb65a6650e91d3f40154d3fff06dcb10091d3fe5816ea42d321` (13,495,289 bytes)
  - `small/customers.csv.gz`: `fd30e1d4970804569e5adf5507a2e05f3079bfbdeaed5eaacfda09782c45f963` (9,922,134 bytes)
  - `small/products.csv.gz`: `f16ffc02c0452bff52617951d8c3c29779b4dc599d41f76d348e279456eace58` (542,682 bytes)
  - `small/tenants.csv.gz`: `350f1d0835119ebd509a032b5127c187791f7ce09d5fb4d438152f9f5574793d` (409 bytes)
- **10M Main Profile**:
  - `main/orders.csv.gz`: `d687eab010b29d24585ab27e18a1491c16f9a491cfd85f65b2dcca0d8133fe6d` (270,473,556 bytes)
  - `main/order_items.csv.gz`: `9549d455c0fec56aa6a07acc8d8118fd3a4c7727fa5edfc9cbaa79a7fbd53dc8` (141,245,860 bytes)
  - `main/customers.csv.gz`: `fd30e1d4970804569e5adf5507a2e05f3079bfbdeaed5eaacfda09782c45f963` (9,922,134 bytes)
  - `main/products.csv.gz`: `f16ffc02c0452bff52617951d8c3c29779b4dc599d41f76d348e279456eace58` (542,682 bytes)
  - `main/tenants.csv.gz`: `350f1d0835119ebd509a032b5127c187791f7ce09d5fb4d438152f9f5574793d` (409 bytes)

### Step 4: Run 1M Setup
```powershell
python scripts/setup.py --profile 1m
```
*Expected measured runtime on our machine*: **~35 to 45 seconds**.

### Step 5: Run Verification Tests (1M)
```powershell
# Run the dataset integrity tripwire
python tests/integrity_tripwire.py 1m

# Run all 11 trust & measurement suites
python tests/run_all.py --profile 1m
```
*Expected measured runtime on our machine*: **~35 seconds** (100% PASS on all 11 suites, 480/480 golden checks passed).

### Step 6: (Optional) Run 10M Setup
When ready to test the 10M dataset:
```powershell
python scripts/setup.py --profile 10m
python tests/integrity_tripwire.py 10m
python tests/run_all.py --profile 10m
```
*Expected measured runtime on our machine*: **~350 to 420 seconds** (split & load ~180s, triggers ~12s, rebuild ~190s).

---

## 3. Expected Measured Times & Outputs

*All measurements below recorded on Windows 11 host (AMD Ryzen 7 7735HS, 16 vCPUs, 16 GB RAM)*:

### Setup 1M (`python scripts/setup.py --profile 1m`):
- Template DB creation: `~0.8s`
- 100 tenant DB cloning: `~2.9s`
- CSV streaming & demuxing: `~4.5s`
- 100 DB COPY, indexes, ANALYZE: `~12.8s`
- Outbox & statement triggers: `~1.8s`
- Initial rebuild: `~14.5s`
- Completeness guard & smoke test: `~2.5s`
- **Total Setup Time**: `~40 seconds`

### Test Suite (`python tests/run_all.py --profile 1m`):
```
======================================================================
TEST SUITE SUMMARY (1M):
  1. Tenant Isolation                          :     PASS (1.42s)
  2. Idempotency per-tenant                    :     PASS (0.19s)
  3. Out-of-Order Commit per-tenant            :     PASS (1.33s)
  4. Single-Tenant Mutation & Fan-out          :     PASS (0.87s)
  5. Concurrent Multi-Tenant Updates (25 DBs)  :     PASS (4.37s)
  6. Fairness & Anti-Starvation                :     PASS (1.35s)
  7. Quarantine Handling                       :     PASS (0.24s)
  8. Crash Recovery per-tenant                 :     PASS (6.01s)
  9. Per-Tenant Fail Closed & Stall Isolation  :     PASS (2.92s)
  10. Rebuild Equality under Load per-tenant   :     PASS (8.06s)
  11. Golden Ground-Truth Parity vs Tenant DBs :     PASS (9.55s)
======================================================================
SUCCESS: All 11 test suites passed with 480/480 golden checks at 1M!
GOLDEN CHECK RESULTS: 480/480 passed. Total mismatches: 0
```

---

## 4. Top 5 Most Likely Failures & Fixes

### 1. Docker Daemon Not Running
- **Symptom**: `[SETUP ERROR] Docker is not running or not accessible!` or `error during connect: ...`.
- **Root Cause**: Docker Desktop is not started or WSL2 engine is not initialized.
- **Fix**: Launch Docker Desktop from the Start Menu. Wait until the whale icon shows "Engine running", then test with `docker ps` before re-running setup.

### 2. Host Port Clashes (Port 5432 / 5433 / 8000 in use)
- **Symptom**: `Bind for 0.0.0.0:5432 failed: port is already allocated`.
- **Root Cause**: A local PostgreSQL or web service is running natively on your Windows laptop.
- **Fix**: Override the host ports via environment variables without touching any code:
  ```powershell
  $env:MAIN_DB_PORT = "15432"
  $env:ROLLUP_DB_PORT = "15433"
  $env:API_PORT = "18000"
  ```
  CyberForge will dynamically bind the containers and connect all scripts to these ports.

### 3. Out of Memory / WSL2 OOM Killed
- **Symptom**: PostgreSQL crashes during 10M parallel COPY or rebuild with `server closed the connection unexpectedly`.
- **Root Cause**: WSL2 default memory limit is 50% of system RAM (8 GB on a 16 GB laptop), causing Linux kernel OOM killer to terminate PostgreSQL.
- **Fix**: Ensure `%USERPROFILE%\.wslconfig` sets `memory=12GB` (see Section 1). Run `wsl --shutdown` and restart Docker Desktop. Also reduce parallel threads if needed:
  ```powershell
  python scripts/setup.py --profile 10m --workers 8
  ```

### 4. CRLF Line Endings in Shell Scripts
- **Symptom**: `/bin/sh^M: bad interpreter: No such file or directory`.
- **Root Cause**: Git on Windows converted LF to CRLF upon checkout.
- **Fix**: The repository includes `.gitattributes` enforcing LF. Run:
  ```powershell
  git checkout-index --force --all
  ```

### 5. Dataset Archive Not Found
- **Symptom**: `[SETUP ERROR] Mitigata dataset not found for profile '1m'!`.
- **Root Cause**: `mitigata_dataset.zip` was not copied or was placed in the wrong folder.
- **Fix**: Place `mitigata_dataset.zip` at `data/mitigata_dataset.zip` or set:
  ```powershell
  $env:DATASET_ZIP = "C:\Users\<user>\Downloads\mitigata_dataset.zip"
  ```
  Re-run `python scripts/setup.py --profile 1m`.

---

## 5. What to Send Back to Balraju

After your run, please send back:
1. **Hardware info**:
   ```powershell
   Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors
   (Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum).Sum / 1GB
   ```
2. **Setup log**: Output from `python scripts/setup.py --profile 1m`.
3. **Run All output**: Output from `python tests/run_all.py --profile 1m`.
4. **Integrity Tripwire output**: Output from `python tests/integrity_tripwire.py 1m`.
5. **Benchmark output**: Output from `python bench/bench.py --profile 1m` (if executed).
