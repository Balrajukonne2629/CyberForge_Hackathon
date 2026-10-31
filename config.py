"""Central configuration loader supporting PROFILE=1m|10m and LAYOUT=per_tenant|single."""
import os, yaml
from typing import Optional, List

def get_config(profile: Optional[str] = None):
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    prof = profile or os.getenv("PROFILE") or cfg.get("profile", "1m")
    prof_data = cfg.get("profiles", {}).get(prof, {})

    layout = os.getenv("LAYOUT") or prof_data.get("layout") or cfg.get("layout", "per_tenant")

    cfg["active_profile"] = prof
    cfg["active_layout"] = layout
    cfg["database"]["main_db_url"] = os.getenv("MAIN_DB_URL", prof_data.get("main_db_url", cfg["database"].get("main_db_url")))
    cfg["database"]["rollup_db_url"] = os.getenv("ROLLUP_DB_URL", prof_data.get("rollup_db_url", cfg["database"].get("rollup_db_url")))
    cfg["database"]["main_db_name"] = prof_data.get("main_db_name", "shop")
    cfg["database"]["rollup_db_name"] = prof_data.get("rollup_db_name", "rollup_1m")
    return cfg

def get_tenant_db_name(tenant_id: int, profile: Optional[str] = None) -> str:
    cfg = get_config(profile)
    prof = cfg["active_profile"]
    prefix = cfg.get("tenant_db", {}).get("name_prefix_10m", "t10m_") if prof == "10m" else cfg.get("tenant_db", {}).get("name_prefix_1m", "tenant_")
    return f"{prefix}{tenant_id:03d}"

def get_tenant_db_url(
    tenant_id: int,
    profile: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None
) -> str:
    cfg = get_config(profile)
    tdb = cfg.get("tenant_db", {})
    h = host or os.getenv("MAIN_DB_HOST", tdb.get("host", "127.0.0.1"))
    p = port or int(os.getenv("MAIN_DB_PORT", tdb.get("port", 5432)))
    u = user or os.getenv("MAIN_DB_USER", tdb.get("user", "postgres"))
    pwd = password or os.getenv("MAIN_DB_PASSWORD", tdb.get("password", "postgres"))
    db_name = get_tenant_db_name(tenant_id, profile)
    return f"postgresql://{u}:{pwd}@{h}:{p}/{db_name}"

def get_all_tenant_ids(count: int = 100) -> List[int]:
    return list(range(1, count + 1))
