"""桩：botpy（QQ 官方机器人 SDK）。

真实环境依赖 qq-botpy；测试里用它来验证「请求构造」是否符合
QQ 机器人开放平台 API v2 文档（方法、路径、请求体），不触网。
"""

from __future__ import annotations

__all__ = ["http"]
