"""桩：``botpy.http.Route``（与上游同签名：Route(method, path, **parameters)）。"""

from __future__ import annotations

from typing import Any


class Route:
    def __init__(self, method: str, path: str, **parameters: Any) -> None:
        self.method = method
        self.path = path
        self.parameters = parameters

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Route({self.method} {self.path} {self.parameters})"


__all__ = ["Route"]
