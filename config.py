"""Central configuration loader supporting PROFILE=1m|10m and LAYOUT=per_tenant|single.
Supports environment variables for dynamic host, port, user, and password configuration.
"""
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

    # Dynamic host/port overrides for main and rollup DBs
    main_host = os.getenv("MAIN_DB_HOST", cfg.get("tenant_db", {}).get("host", "127.0.0.1"))
    main_port = int(os.getenv("MAIN_DB_PORT", cfg.get("tenant_db", {}).get("port", 5432)))
    main_user = os.getenv("MAIN_DB_USER", cfg.get("tenant_db", {}).get("user", "postgres"))
    main_pwd = os.getenv("MAIN_DB_PASSWORD", cfg.get("tenant_db", {}).get("password", "postgres"))
    main_name = prof_data.get("main_db_name", "shop" if prof == "1m" else "shop_10m")

    rollup_host = os.getenv("ROLLUP_DB_HOST", "127.0.0.1")
    rollup_port = int(os.getenv("ROLLUP_DB_PORT", 5433))
    rollup_user = os.getenv("ROLLUP_DB_USER", "postgres")
    rollup_pwd = os.getenv("ROLLUP_DB_PASSWORD", "postgres")
    rollup_name = prof_data.get("rollup_db_name", "rollup_1m" if prof == "1m" else "rollup_10m")

    default_main_url = f"postgresql://{main_user}:{main_pwd}@{main_host}:{main_port}/{main_name}"
    default_rollup_url = f"postgresql://{rollup_user}:{rollup_pwd}@{rollup_host}:{rollup_port}/{rollup_name}"

    if "MAIN_DB_URL" in os.environ:
        cfg["database"]["main_db_url"] = os.environ["MAIN_DB_URL"]
    elif "MAIN_DB_PORT" in os.environ or "MAIN_DB_HOST" in os.environ or "MAIN_DB_USER" in os.environ:
        cfg["database"]["main_db_url"] = default_main_url
    else:
        cfg["database"]["main_db_url"] = prof_data.get("main_db_url", default_main_url)

    if "ROLLUP_DB_URL" in os.environ:
        cfg["database"]["rollup_db_url"] = os.environ["ROLLUP_DB_URL"]
    elif "ROLLUP_DB_PORT" in os.environ or "ROLLUP_DB_HOST" in os.environ or "ROLLUP_DB_USER" in os.environ:
        cfg["database"]["rollup_db_url"] = default_rollup_url
    else:
        cfg["database"]["rollup_db_url"] = prof_data.get("rollup_db_url", default_rollup_url)

    cfg["database"]["main_db_name"] = main_name
    cfg["database"]["rollup_db_name"] = rollup_name
    return cfg

def get_admin_db_url(
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    db_name: str = "postgres"
) -> str:
    cfg = get_config()
    tdb = cfg.get("tenant_db", {})
    h = host or os.getenv("MAIN_DB_HOST", tdb.get("host", "127.0.0.1"))
    p = port or int(os.getenv("MAIN_DB_PORT", tdb.get("port", 5432)))
    u = user or os.getenv("MAIN_DB_USER", tdb.get("user", "postgres"))
    pwd = password or os.getenv("MAIN_DB_PASSWORD", tdb.get("password", "postgres"))
    return f"postgresql://{u}:{pwd}@{h}:{p}/{db_name}"

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
