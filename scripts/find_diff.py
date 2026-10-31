import psycopg2, os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config

cfg = get_config("1m")
conn_m = psycopg2.connect(cfg["database"]["main_db_url"])
conn_r = psycopg2.connect(cfg["database"]["rollup_db_url"])
cur_m = conn_m.cursor()
cur_r = conn_r.cursor()

cur_m.execute("SELECT tenant_id, date_trunc('hour', created_at) as hour, status, count(*) FROM orders GROUP BY 1, 2, 3")
m_map = {(t, h, s): c for t, h, s, c in cur_m.fetchall()}

cur_r.execute("SELECT tenant_id, hour, status, order_count FROM rollup_orders_hour")
r_map = {(t, h, s): c for t, h, s, c in cur_r.fetchall()}

diffs = [("diff", k, m_map.get(k, 0), r_map.get(k, 0)) for k in set(m_map) | set(r_map) if m_map.get(k, 0) != r_map.get(k, 0)]
print(f"Total diff keys: {len(diffs)}")
for d in diffs[:10]:
    print(d)
