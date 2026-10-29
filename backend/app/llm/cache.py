"""Disk cache of model responses, keyed by a hash of everything that determines the answer.

Key = sha256(provider, model, purpose, prompt version, schema, messages incl. image bytes).
It saves credits, makes evaluations reproducible, and lets the demo run offline (CACHE_ONLY=true).
"""

import hashlib
import json
import os
from pathlib import Path


class ResponseCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def key(**parts) -> str:
        blob = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None                      # a corrupt entry is a miss, never a crash

    def put(self, key: str, value: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)                # atomic: readers never see half a file
