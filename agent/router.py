"""端云路由策略。

Phase 1：云端（视觉）每步都做规划，端侧只做 grounding，所以这里的职责
简化为「卡死检测」。
Phase 2（待做）：
  - needs_cloud_planning(): 高置信度简单步走端侧快速通道，省一次云端调用；
  - 本地 grounding 失败时升级云端视觉重新定位。
"""
from __future__ import annotations

from .config import STUCK_THRESHOLD
from .memory.context import History


class Router:
    def __init__(self, stuck_threshold: int = STUCK_THRESHOLD) -> None:
        self.stuck_threshold = stuck_threshold

    def should_abort(self, history: History) -> bool:
        """连续多步页面无进展 → 判定卡死，提前终止避免空转。"""
        return history.stuck_count() >= self.stuck_threshold
