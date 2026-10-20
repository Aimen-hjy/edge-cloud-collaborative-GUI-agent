"""主循环：observe → plan(云) → ground(端) → act → verify。

端云分工：
  云端 qwen3-vl-max  看截图决策「做什么 + 目标描述」
  端侧 qwen3-0.6b    把目标描述定位到控件坐标
  adb                执行动作
"""
from __future__ import annotations

import time
from typing import Optional

from .config import ACTION_SETTLE_SEC, CLOUD_ENDPOINT, LOCAL_ENDPOINT, MAX_STEPS
from .device import adb, screen
from .device.screen import UIElement
from .llm.client import LLMClient, extract_json, image_b64, image_msg, text_msg
from .llm.prompts import PLANNER_SYSTEM, PLANNER_USER_TEMPLATE
from .memory.context import History
from .perception.ground import ground, ground_visual
from .router import Router
from .tools.actions import ActionExecutor


def run_task(task: str, client: Optional[LLMClient] = None,
             executor: Optional[ActionExecutor] = None,
             verbose: bool = True) -> dict:
    """执行一个 GUI 任务，返回 {success, reason, steps}。"""
    client = client or LLMClient()
    executor = executor or ActionExecutor()
    history = History()
    router = Router()

    # 换任务先清端侧 KV Cache，避免上一轮对话污染
    client.reset_kv(LOCAL_ENDPOINT)

    # 首次抓屏，顺便探测设备是否就绪
    shot = adb.screencap()
    if not shot:
        return _result(False, "截图失败：请确认设备已连接（adb devices）", 0)

    for step in range(1, MAX_STEPS + 1):
        xml = adb.dump_ui()
        elements = screen.parse_ui(xml)

        # ── 1. 云端规划（看图 + 任务 + 历史 → 下一步动作）──
        user = PLANNER_USER_TEMPLATE.format(
            task=task, history=history.to_text() or "（无）")
        plan_msg = client.chat(
            CLOUD_ENDPOINT,
            [text_msg("system", PLANNER_SYSTEM),
             image_msg("user", user, image_b64(shot))],
            temperature=0.0, max_tokens=512,
        )
        plan = extract_json(plan_msg.content)
        if not isinstance(plan, dict):
            return _result(False, "云端输出无法解析", step)

        action = plan.get("action")
        target = plan.get("target")
        if action == "done":
            return _result(True, plan.get("reason", "完成"), step)
        if action == "fail":
            return _result(False, plan.get("reason", "无法完成"), step)

        # ── 2. 执行（端侧 grounding → adb 注入）──────────
        status = _execute(action, plan, target, elements, shot, client, executor)
        if status == "unknown":
            return _result(False, f"未知动作类型: {action}", step)

        # ── 3. 校验：等待 UI 稳定后对比页面指纹 ──────────
        time.sleep(ACTION_SETTLE_SEC)
        new_xml = adb.dump_ui()
        changed = screen.signature(new_xml) != screen.signature(xml)
        history.add(str(action), target or "", changed)

        if verbose:
            print(f"[step {step:02d}] {action} {target or ''} "
                  f"→ {'页面变化' if changed else '页面无变化'} ({status})")

        if router.should_abort(history):
            return _result(False, f"连续 {router.stuck_threshold} 步无进展，任务中止", step)

        # 下一步继续抓当前屏
        shot = adb.screencap()

    return _result(False, "达到最大步数上限", MAX_STEPS)


def _execute(action: str, plan: dict, target: Optional[str],
             elements: list[UIElement], shot: bytes, client: LLMClient,
             executor: ActionExecutor) -> str:
    """执行单个动作，返回状态码：
    ok / no_target / bad_coord / bad_action / unknown
    """
    if action in ("tap", "type"):
        coord = _locate(target, elements, shot, client, executor)
        if coord is None:
            return "no_target"
        if action == "tap":
            return "ok" if executor.tap(*coord) else "bad_coord"
        return "ok" if executor.type_coord(*coord, plan.get("text", "")) else "bad_coord"

    if action == "swipe":
        return "ok" if executor.swipe(plan.get("direction", "up")) else "bad_action"

    if action == "back":
        executor.back()
        return "ok"

    if action == "home":
        executor.home()
        return "ok"

    if action == "launch":
        pkg = plan.get("package", "")
        if not pkg:
            return "bad_action"
        executor.launch(pkg)
        return "ok"

    return "unknown"


def _locate(target: Optional[str], elements: list[UIElement], shot: bytes,
            client: LLMClient, executor: ActionExecutor):
    """定位目标控件中心坐标：优先 UI 树文本 grounding（快），失败再视觉 grounding（截图）。"""
    el = ground(client, target or "", elements)
    if el is not None:
        return el.center
    if shot:
        return ground_visual(client, target or "", shot, executor._w, executor._h)
    return None


def _result(success: bool, reason: str, steps: int) -> dict:
    return {"success": success, "reason": reason, "steps": steps}
