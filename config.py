"""Configuration management.

Stores Odoo connection settings in config.json with optional .env /
environment variable overrides for sensitive values.
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data"))
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "odoo": {
        "url": "",
        "db": "",
        "user": "",
        "password": "",
    },
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            config = _deep_merge(DEFAULT_CONFIG, saved)
        except Exception as e:
            logger.error("Failed to load config: %s", e)
            config = json.loads(json.dumps(DEFAULT_CONFIG))
    else:
        config = json.loads(json.dumps(DEFAULT_CONFIG))

    _env_overrides = {
        ("odoo", "url"):      "ODOO_URL",
        ("odoo", "db"):       "ODOO_DB",
        ("odoo", "user"):     "ODOO_USER",
        ("odoo", "password"): "ODOO_PASSWORD",
    }
    for (section, key), env_var in _env_overrides.items():
        val = os.getenv(env_var)
        if val:
            config[section][key] = val

    return config


def save_config(config: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    logger.info("Config saved")


def is_odoo_configured(config: dict) -> bool:
    o = config.get("odoo", {})
    return bool(o.get("url") and o.get("db") and o.get("user") and o.get("password"))


def _deep_merge(default: dict, override: dict) -> dict:
    result = default.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
