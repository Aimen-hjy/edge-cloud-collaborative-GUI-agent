"""动作执行器：把高层动作映射成 adb 注入，并在执行前做坐标合法性校验。

0.6B 可能给出越界坐标，这里先拦一道，避免注入到屏幕外。
"""
from __future__ import annotations

from ..device import adb
from ..device.screen import UIElement


class ActionExecutor:
    def __init__(self) -> None:
        self._w, self._h = adb.screen_size()

    def _in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x <= self._w and 0 <= y <= self._h

    def tap(self, x: int, y: int) -> bool:
        if not self._in_bounds(x, y):
            return False
        adb.tap(x, y)
        return True

    def tap_element(self, el: UIElement) -> bool:
        return self.tap(*el.center)

    def type(self, el: UIElement, text: str) -> bool:
        """先点输入框聚焦，再注入文本。"""
        if not self.tap_element(el):
            return False
        adb.input_text(text)
        return True

    def type_coord(self, x: int, y: int, text: str) -> bool:
        """按坐标点输入框聚焦，再注入文本（视觉 grounding 路径用）。"""
        if not self.tap(x, y):
            return False
        adb.input_text(text)
        return True

    def swipe(self, direction: str = "up") -> bool:
        w, h = self._w, self._h
        cx, cy = w // 2, h // 2
        mapping = {
            "up":    (cx, int(h * 0.8), cx, int(h * 0.2)),
            "down":  (cx, int(h * 0.2), cx, int(h * 0.8)),
            "left":  (int(w * 0.8), cy, int(w * 0.2), cy),
            "right": (int(w * 0.2), cy, int(w * 0.8), cy),
        }
        if direction not in mapping:
            return False
        adb.swipe(*mapping[direction])
        return True

    def back(self) -> None:
        adb.back()

    def home(self) -> None:
        adb.home()

    def launch(self, package: str) -> None:
        adb.launch(package)
