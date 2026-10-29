"""UI 树确定性定位 + 点按：不调模型，纯文本匹配。

「点击确定」「点 文件传输助手」这类任务，dump 出 UI 树后按 text / content-desc /
resource-id 直接匹配控件，读 bounds 中心 tap。比云端 VLM grounding 快一个数量级，
比端侧 0.6B 文本 grounding 省掉整个 prefill。
"""
from __future__ import annotations

from typing import Optional

from . import adb
from .screen import UIElement, parse_ui


def find_element(target: str, elements: list[UIElement]) -> Optional[UIElement]:
    """目标文字 → 控件。优先级：精确 > 包含，text > content-desc > resource-id。"""
    if not target or not elements:
        return None
    t = target.strip()
    # 1) text 精确
    for e in elements:
        if e.text == t:
            return e
    # 2) content-desc 精确
    for e in elements:
        if e.desc == t:
            return e
    # 3) text 包含（双向，容忍「确定 / 确定按钮」这类包装）
    for e in elements:
        if e.text and (t in e.text or e.text in t):
            return e
    # 4) content-desc 包含
    for e in elements:
        if e.desc and (t in e.desc or e.desc in t):
            return e
    # 5) resource-id 尾部（去包名前缀）包含
    for e in elements:
        rid = e.resource_id.rsplit("/", 1)[-1] if e.resource_id else ""
        if rid and (t in rid or rid in t):
            return e
    return None


def tap_by_text(target: str, max_elements: int = 300) -> Optional[str]:
    """按文字点按控件。成功返回人话结果；找不到返回 None（上层升级云端）。"""
    elements = parse_ui(adb.dump_ui(), max_elements=max_elements)
    el = find_element(target, elements)
    if el is None:
        return None
    adb.tap(*el.center)
    return f"已点击「{el.label}」"
