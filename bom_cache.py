"""BOM structure cache.

Caches BOM explosion results (tree + flat) as JSON on disk.
BOMs are stored normalized to qty=1.0 and scaled at query time.
Stock levels are never cached.
"""

import json
import logging
import time
from pathlib import Path

from config import DATA_DIR

logger = logging.getLogger(__name__)

CACHE_FILE = DATA_DIR / "bom_cache.json"
DEFAULT_TTL = 86400  # 24 hours


class BomCache:
    def __init__(self, ttl: int = DEFAULT_TTL):
        self._ttl = ttl
        self._cache: dict = self._load_from_disk()

    def _load_from_disk(self) -> dict:
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                if data.get("version") == 1 and time.time() < data.get("expires_at", 0):
                    logger.info("BOM cache loaded: %d entries", len(data.get("boms", {})))
                    return data
                logger.info("BOM cache expired, starting fresh")
            except Exception as e:
                logger.warning("Failed to load BOM cache: %s", e)
        return self._empty_cache()

    def _empty_cache(self) -> dict:
        now = time.time()
        return {"version": 1, "created_at": now, "expires_at": now + self._ttl, "boms": {}}

    def _save_to_disk(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump(self._cache, f)
        except Exception as e:
            logger.error("Failed to save BOM cache: %s", e)

    def is_valid(self) -> bool:
        return time.time() < self._cache.get("expires_at", 0)

    def get_bom(self, template_id: int) -> dict | None:
        if not self.is_valid():
            self._cache = self._empty_cache()
            return None
        return self._cache["boms"].get(str(template_id))

    def set_bom(self, template_id: int, tree: list, flat: list) -> None:
        if not self.is_valid():
            self._cache = self._empty_cache()
        self._cache["boms"][str(template_id)] = {
            "tree": tree,
            "flat": flat,
            "cached_at": time.time(),
        }
        self._save_to_disk()
        logger.info("Cached BOM for template_id=%d", template_id)

    def invalidate_all(self) -> None:
        self._cache = self._empty_cache()
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
        logger.info("BOM cache cleared")

    def get_info(self) -> dict:
        bom_count = len(self._cache.get("boms", {}))
        expires_at = self._cache.get("expires_at", 0)
        remaining = max(0, expires_at - time.time())
        return {
            "bom_count": bom_count,
            "expires_in_hours": round(remaining / 3600, 1),
            "valid": self.is_valid(),
        }
