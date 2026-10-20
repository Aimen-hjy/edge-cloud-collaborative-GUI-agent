"""执行历史：记录每步 (动作, 目标, 页面是否变化)，供云端规划与卡死检测用。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Step:
    action: str
    target: str
    changed: bool  # 动作后页面是否有变化

    def to_text(self) -> str:
        flag = "页面变化" if self.changed else "页面无变化"
        if self.target:
            return f"{self.action} '{self.target}' ({flag})"
        return f"{self.action} ({flag})"


class History:
    def __init__(self, max_keep: int = 8) -> None:
        self.steps: list[Step] = []
        self._max_keep = max_keep

    def add(self, action: str, target: str, changed: bool) -> None:
        self.steps.append(Step(action, target or "", changed))

    def to_text(self) -> str:
        """只保留最近几步，避免上下文无限增长。"""
        recent = self.steps[-self._max_keep:]
        return "\n".join(f"{i + 1}. {s.to_text()}" for i, s in enumerate(recent))

    def stuck_count(self) -> int:
        """末尾连续「页面无变化」的步数。"""
        count = 0
        for s in reversed(self.steps):
            if not s.changed:
                count += 1
            else:
                break
        return count
