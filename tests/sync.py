"""Synchronization helper for test verification."""
import time, psycopg2

def wait_for_worker(main_url: str, rollup_url: str, timeout: float = 15.0):
    t0 = time.time()
    main_conn = psycopg2.connect(main_url)
    main_conn.autocommit = True
    main_cur = main_conn.cursor()

    rollup_conn = psycopg2.connect(rollup_url)
    rollup_conn.autocommit = True
    rollup_cur = rollup_conn.cursor()

    main_cur.execute("SELECT COALESCE(MAX(id), 0) FROM outbox")
    max_id = main_cur.fetchone()[0]

    while time.time() - t0 < timeout:
        rollup_cur.execute("SELECT cursor_id, worker_stalled FROM worker_state WHERE id = 1")
        row = rollup_cur.fetchone()
        if row:
            cur_id, stalled = row
            if stalled:
                main_cur.close()
                main_conn.close()
                rollup_cur.close()
                rollup_conn.close()
                raise RuntimeError("Worker is stalled during sync!")
            if cur_id >= max_id:
                break
        time.sleep(0.05)

    main_cur.close()
    main_conn.close()
    rollup_cur.close()
    rollup_conn.close()
