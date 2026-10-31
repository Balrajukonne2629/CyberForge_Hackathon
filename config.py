"""Central configuration loader supporting PROFILE=1m|10m."""
import os, yaml

def get_config(profile: str = None):
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    prof = profile or os.getenv("PROFILE") or cfg.get("profile", "1m")
    prof_data = cfg.get("profiles", {}).get(prof, {})

    cfg["active_profile"] = prof
    cfg["database"]["main_db_url"] = os.getenv("MAIN_DB_URL", prof_data.get("main_db_url", cfg["database"].get("main_db_url")))
    cfg["database"]["rollup_db_url"] = os.getenv("ROLLUP_DB_URL", prof_data.get("rollup_db_url", cfg["database"].get("rollup_db_url")))
    cfg["database"]["main_db_name"] = prof_data.get("main_db_name", "shop")
    cfg["database"]["rollup_db_name"] = prof_data.get("rollup_db_name", "rollup_1m")
    return cfg
