"""UIAutomator XML → 结构化控件列表。

Grounding 的核心输入：把 XML 抽成带编号的控件，端侧文本模型靠这个定位坐标。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class UIElement:
    index: int
    text: str
    desc: str
    resource_id: str
    cls: str
    clickable: bool
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    @property
    def label(self) -> str:
        """优先用有辨识度的字段，作为控件的人类可读名。"""
        return self.text or self.desc or self.resource_id or self.cls


def parse_ui(xml_str: str, max_elements: int = 80) -> list[UIElement]:
    """解析 UI 层级，返回按阅读顺序（先 y 后 x）排序、编号的控件列表。

    只保留「可点击 或 有文本/描述」的节点，并截断到 max_elements，
    避免控件树过大撑爆端侧 0.6B 的上下文。
    """
    if not xml_str:
        return []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []

    elems: list[UIElement] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            text = (child.get("text") or "").strip()
            desc = (child.get("content-desc") or "").strip()
            rid = child.get("resource-id") or ""
            cls = child.get("class") or ""
            clickable = child.get("clickable") == "true"
            m = _BOUNDS_RE.search(child.get("bounds") or "")
            if m and (clickable or text or desc):
                elems.append(UIElement(
                    index=0, text=text, desc=desc, resource_id=rid, cls=cls,
                    clickable=clickable,
                    x1=int(m.group(1)), y1=int(m.group(2)),
                    x2=int(m.group(3)), y2=int(m.group(4)),
                ))
            walk(child)

    walk(root)
    # 阅读顺序排序（从上到下、从左到右），让编号稳定
    elems.sort(key=lambda e: (e.y1, e.x1))
    elems = elems[:max_elements]
    for i, e in enumerate(elems):
        e.index = i
    return elems


def to_prompt(elems: list[UIElement]) -> str:
    """控件列表 → 端侧模型可读的文本。"""
    lines = []
    for e in elems:
        cx, cy = e.center
        lines.append(
            f"[{e.index}] '{e.label}' center=({cx},{cy}) clickable={e.clickable}"
        )
    return "\n".join(lines)


def signature(xml_str: str) -> tuple[str, ...]:
    """屏幕内容指纹，用于判断一次动作后页面是否发生变化。"""
    return tuple(e.label for e in parse_ui(xml_str))
