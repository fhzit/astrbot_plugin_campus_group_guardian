"""桩：``from astrbot.core.utils.astrbot_path import get_astrbot_data_path``。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def get_astrbot_data_path() -> Path:
    override = os.environ.get("ASTRBOT_DATA_PATH")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "astrbot-stub-data"


__all__ = ["get_astrbot_data_path"]
