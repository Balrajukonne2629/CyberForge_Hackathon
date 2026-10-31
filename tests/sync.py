"""Synchronization helper for multi-tenant test verification."""
import time, psycopg2
from typing import Optional, List
from config import get_config, get_tenant_db_url, get_all_tenant_ids

def wait_for_worker(
    main_url_or_profile: str = "1m",
    rollup_url: Optional[str] = None,
    tenant_id: Optional[int] = None,
    timeout: float = 15.0
):
    # Support both old signature wait_for_worker(main_url, rollup_url) and new profile-based
    if "postgresql://" in str(main_url_or_profile):
        cfg = get_config("1m")
        prof = cfg["active_profile"]
    else:
        prof = str(main_url_or_profile)
        cfg = get_config(prof)

    r_url = rollup_url or cfg["database"]["rollup_db_url"]
    target_tids: List[int] = [tenant_id] if tenant_id is not None else get_all_tenant_ids(100)

    from concurrent.futures import ThreadPoolExecutor

    # Collect max outbox id per target tenant in parallel
    def fetch_max_id(tid: int):
        try:
            t_url = get_tenant_db_url(tid, prof)
            tc = psycopg2.connect(t_url)
            tcur = tc.cursor()
            tcur.execute("SELECT COALESCE(MAX(id), 0) FROM outbox")
            m = tcur.fetchone()[0]
            tcur.close()
            tc.close()
            return tid, m
        except Exception:
            return tid, 0

    max_ids = {}
    with ThreadPoolExecutor(max_workers=25) as executor:
        for tid, m in executor.map(fetch_max_id, target_tids):
            max_ids[tid] = m

    r_conn = psycopg2.connect(r_url)
    r_conn.autocommit = True
    r_cur = r_conn.cursor()

    t0 = time.time()
    all_caught_up = False
    states = {}
    while time.time() - t0 < timeout:
        r_cur.execute("""
            SELECT tenant_id, cursor_id, stalled, last_error 
            FROM worker_state 
            WHERE tenant_id = ANY(%s)
        """, (target_tids,))
        states = {r[0]: (r[1], bool(r[2]), r[3]) for r in r_cur.fetchall()}

        all_caught_up = True
        for tid, expected_max in max_ids.items():
            if expected_max == 0:
                continue
            cur_id, stalled, err = states.get(tid, (0, False, None))
            if stalled:
                r_cur.close()
                r_conn.close()
                raise RuntimeError(f"Worker stalled for tenant {tid}: {err}")
            if cur_id < expected_max:
                all_caught_up = False
                break

        if all_caught_up:
            break
        time.sleep(0.05)

    r_cur.close()
    r_conn.close()

    if not all_caught_up:
        raise TimeoutError(f"wait_for_worker timed out after {timeout}s: states={states}, expected_max={max_ids}")
