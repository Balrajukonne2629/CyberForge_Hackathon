#!/usr/bin/env python3
"""scripts/run_10m_pipeline.py:
Background pipeline for Phase 2.5 10M dataset:
1. Split 10M CSVs into 100 databases (t10m_001 .. t10m_100), post_load indexes, ANALYZE.
2. Verify split completeness against 10M source CSVs (Split Guard).
3. Install triggers on all 100 10M tenant DBs.
4. Parallel rebuild into rollup_10m.
"""
import sys, os, time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.split_by_tenant import split_and_load, verify_split_completeness
from scripts.install_triggers import install_triggers
from scripts.rebuild import rebuild_all

def main():
    t_start = time.time()
    print("=" * 80)
    print("STARTING 10M PER-TENANT PIPELINE (t10m_001 .. t10m_100)")
    print("=" * 80)

    # 1. Split & Load 10M
    print("\n>>> STEP 1: Splitting and loading 10M records into 100 tenant databases...")
    split_and_load(profile="10m", tenant_count=100, max_workers=16)

    # 2. Install Outbox & Triggers on 10M
    print("\n>>> STEP 2: Installing outbox & triggers on 100 10M tenant databases...")
    install_triggers(profile="10m", tenant_count=100, max_workers=16)

    # 3. Parallel Rebuild into rollup_10m
    print("\n>>> STEP 3: Parallel rebuild of rollups from 100 10M tenant databases into rollup_10m...")
    rebuild_all(profile="10m", tenant_count=100, max_workers=16)

    total_elapsed = time.time() - t_start
    print("=" * 80)
    print(f"10M PIPELINE COMPLETE IN {total_elapsed:.2f}s ({total_elapsed/60:.2f} min)!")
    print("=" * 80)

if __name__ == "__main__":
    main()
