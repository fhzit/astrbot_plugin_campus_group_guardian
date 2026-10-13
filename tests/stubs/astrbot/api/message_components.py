"""桩：``astrbot.api.message_components``。"""

from __future__ import annotations

from typing import Any


class Plain:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Plain({self.text!r})"


class Image:
    def __init__(self, file: str | None = None, url: str | None = None, **kwargs: Any) -> None:
        self.file = file
        self.url = url
        self.extra = kwargs


class Reply:
    def __init__(self, id: str | None = None, **kwargs: Any) -> None:
        self.id = id
        self.extra = kwargs


class File:
    def __init__(self, name: str | None = None, url: str | None = None, **kwargs: Any) -> None:
        self.name = name
        self.url = url
        self.extra = kwargs


__all__ = ["File", "Image", "Plain", "Reply"]
