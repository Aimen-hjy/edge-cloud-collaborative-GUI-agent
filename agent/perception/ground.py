"""端侧 grounding：目标描述 + UI 树 → 控件坐标。

这是端侧 0.6B 的核心价值：云端只给"要操作什么"（中文描述），
端侧负责把描述定位到具体控件编号，再映射成屏幕坐标。
"""
from __future__ import annotations

import re
from typing import Optional

from ..config import LOCAL_ENDPOINT
from ..device.screen import UIElement, to_prompt
from ..llm.client import LLMClient, extract_json, image_msg, text_msg
from ..llm.prompts import (
    GROUNDER_SYSTEM,
    GROUNDER_USER_TEMPLATE,
    GROUNDER_VL_SYSTEM,
    GROUNDER_VL_USER_TEMPLATE,
)


def ground(
    client: LLMClient,
    target: str,
    elements: list[UIElement],
    endpoint=LOCAL_ENDPOINT,
) -> Optional[UIElement]:
    """定位目标控件。找不到时返回 None（触发上层重试/升级）。"""
    if not elements or not target:
        return None

    user = GROUNDER_USER_TEMPLATE.format(target=target, elements=to_prompt(elements))
    messages = [
        text_msg("system", GROUNDER_SYSTEM),
        text_msg("user", user),
    ]
    msg = client.chat(endpoint, messages, temperature=0.0, max_tokens=64)
    data = extract_json(msg.content)
    if not isinstance(data, dict):
        return None

    eid = data.get("element_id")
    if eid is None:
        return None
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        return None
    if 0 <= eid < len(elements):
        return elements[eid]
    return None


def ground_visual(
    client: LLMClient,
    target: str,
    shot_bytes: bytes,
    screen_w: int = 1080,
    screen_h: int = 2340,
    endpoint=LOCAL_ENDPOINT,
):
    """视觉 grounding：截图 + 目标描述 → 屏幕坐标。

    端侧 Qwen3-VL 视觉模型走原生 box 格式 <box_start>(x1,y1),(x2,y2)<box_end>，
    坐标是 0~1000 归一化值。返回边界框中心的 (x, y) 像素坐标；找不到返回 None。
    """
    if not shot_bytes or not target:
        return None

    messages = [
        text_msg("system", GROUNDER_VL_SYSTEM),
        image_msg("user", GROUNDER_VL_USER_TEMPLATE.format(target=target),
                  shot_bytes),
    ]
    msg = client.chat(endpoint, messages, temperature=0.3, max_tokens=64)
    m = _BOX_RE.search(msg.content or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    if x1 == x2 and y1 == y2:
        return None  # 退化成单点，视为定位失败
    px = int((x1 + x2) / 2 / 1000 * screen_w)
    py = int((y1 + y2) / 2 / 1000 * screen_h)
    return px, py


_BOX_RE = re.compile(r"\((\d+),(\d+)\),\((\d+),(\d+)\)")
