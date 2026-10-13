"""桩：``astrbot.api.web``。"""

from __future__ import annotations

import json
from typing import Any


class _Query(dict):
    def get(self, key: str, default: Any = None, type: Any = None) -> Any:  # noqa: A002
        value = dict.get(self, key, default)
        if value is None or type is None:
            return value
        try:
            return type(value)
        except Exception:
            return default


class _Request:
    def __init__(self) -> None:
        self.query = _Query()
        self.username = "tester"
        self._json: dict[str, Any] = {}

    async def json(self, default: Any = None) -> Any:
        return self._json or (default if default is not None else {})

    def set_json(self, payload: dict[str, Any]) -> None:
        self._json = dict(payload)


request = _Request()


def json_response(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def error_response(message: str, status_code: int = 400) -> dict[str, Any]:
    return {"status": "error", "message": message, "status_code": status_code}


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


__all__ = ["dumps", "error_response", "json_response", "request"]
