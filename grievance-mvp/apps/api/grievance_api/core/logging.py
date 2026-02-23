from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict


_RESERVED_LOG_RECORD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj: Dict[str, Any] = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_FIELDS or key.startswith("_"):
                continue
            if key in {"message", "asctime"}:
                continue
            try:
                json.dumps(value, ensure_ascii=False)
                obj[key] = value
            except TypeError:
                obj[key] = str(value)
        if record.exc_info:
            obj["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)

def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    configured_level = getattr(logging, str(level).upper(), logging.INFO)
    if not isinstance(configured_level, int):
        configured_level = logging.INFO
    root.setLevel(configured_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
