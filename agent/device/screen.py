"""UIAutomator XML → 结构化控件列表。

Grounding 的核心输入：把 XML 抽成带编号的控件，端侧文本模型靠这个定位坐标。
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from PIL import Image

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


def img_signature(png_bytes: bytes, grid: int = 8) -> tuple[int, ...]:
    """截图粗粒度指纹：把 PNG 缩成 grid×grid 灰度图。

    用于没有 UI 树时判断页面是否变化。粗粒度缩放能容忍状态栏时钟、
    图标微动等噪声，只对真正的页面跳转敏感。
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    img = img.resize((grid, grid))
    return tuple(img.getdata())


def resize_png(png_bytes: bytes, max_side: int = 1024) -> bytes:
    """把 PNG 长边压到 max_side 像素，返回新的 PNG 字节。

    云端 VL 模型按像素切 image token，原图（1600×2400）token 量是压缩后
    的 4~6 倍，直接拖慢 prefill 和上传。坐标用 0~1000 归一化，与分辨率无关，
    所以缩图基本不损接地精度。长边不超过 max_side 时原样返回。
    """
    img = Image.open(io.BytesIO(png_bytes))
    w, h = img.size
    if max(w, h) <= max_side:
        return png_bytes
    scale = max_side / max(w, h)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def img_changed(before: bytes, after: bytes, tol: float = 0.05) -> bool:
    """两张截图是否发生了明显变化（变化像素占比 > tol）。"""
    a = img_signature(before)
    b = img_signature(after)
    if not a or len(a) != len(b):
        return True
    diff = sum(1 for pa, pb in zip(a, b) if abs(pa - pb) > 16)
    return diff / len(a) > tol
