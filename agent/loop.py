"""主循环：observe → plan(云) → ground(端) → act → verify。

端云分工：
  云端 qwen3-vl-max  看截图决策「做什么 + 目标描述」
  端侧 qwen3-0.6b    把目标描述定位到控件坐标
  adb                执行动作
"""
from __future__ import annotations

import time
from typing import Optional

from .config import ACTION_SETTLE_SEC, CLOUD_ENDPOINT, CLOUD_IMG_MAX_SIDE, MAX_STEPS
from .device import adb, screen, system_query
from .device.screen import UIElement
from .llm.client import LLMClient, extract_json, image_msg, text_msg
from .llm.prompts import PLANNER_SYSTEM, PLANNER_USER_TEMPLATE
from .memory.context import History
from .perception.ground import ground, ground_visual
from .router import Router
from .tools.actions import ActionExecutor
from .tools.system_ops import launch_leading_app, route_with_model, try_local_intent


def run_task(task: str, client: Optional[LLMClient] = None,
             executor: Optional[ActionExecutor] = None,
             verbose: bool = True) -> dict:
    """执行一个 GUI 任务，返回 {success, reason, steps}。

    三层路由（按需付费，不为不需要智能的步骤付模型/UI 树成本）：
      层1 本地系统查询表（dumpsys）：看电量/时间/存储等，<0.2s 直接返回；
      层2 本地模型系统路由：关闭 WiFi/调亮度等系统操作，~1s，失败升级云端；
      层3 云端规划：看图决定动作，back/home/launch/swipe 不抓 UI 树；
      层4 执行：仅 tap/type 才惰性抓 UI 树 + grounding。
    """
    client = client or LLMClient()
    executor = executor or ActionExecutor()
    history = History()
    router = Router()

    # ── 层1：系统查询短路（不抓屏、不调模型）────────────
    ans = system_query.try_answer(task)
    if ans is not None:
        return {"success": True, "reason": ans, "steps": 0}

    # ── 层1.5：关键词系统操作 + 打开App 本地短路（确定性，<0.2s）──
    # 覆盖「关闭蓝牙」「调亮屏幕」「打开相机」等高频任务，比 0.6B 模型路由更稳更快。
    ans = try_local_intent(task)
    if ans is not None:
        return {"success": True, "reason": ans, "steps": 0}

    # ── 层1.6：复合任务拆解「打开X，做Y」→ 本地拉起 X，剩余 Y 交给云端 ──
    remaining = launch_leading_app(task)
    if remaining is not None:
        task = remaining

    # 注意：这里不再 reset_kv。路由请求走 prefix_cache（agent_server 端把恒定的
    # system 前缀 KV 保留复用），reset 会把这份前缀缓存也清掉，反而拖慢每轮路由。

    # ── 层2：本地模型系统路由（处理关键词表接不住的变化说法）──
    ans = route_with_model(task, client)
    if ans is not None:
        return {"success": True, "reason": ans, "steps": 0}

    # 首次抓屏，顺便探测设备是否就绪
    shot = adb.screencap_fast()
    if not shot:
        return _result(False, "截图失败：请确认设备已连接（adb devices）", 0)
    prev_shot: Optional[bytes] = None  # 上一步云端规划所用的截图

    for step in range(1, MAX_STEPS + 1):
        # ── 快路径：屏幕和上一步一模一样 → 跳过云端 ──────
        # 上一步动作没生效（点空/还在加载），云端看到同一张图多半给出同样的动作，
        # 重复问它只白烧 ~4s。这里改为等一拍重截图，让加载中的页面先跑完；
        # 若始终不变，靠 should_abort 连续「无进展」计数提前止损。
        if prev_shot is not None and not screen.img_changed(prev_shot, shot):
            time.sleep(ACTION_SETTLE_SEC)
            shot = adb.screencap_fast()
            history.add("wait", "", False)
            if verbose:
                print(f"[step {step:02d}] wait（屏幕无变化，跳过云端）")
            if router.should_abort(history):
                return _result(False, f"连续 {router.stuck_threshold} 步无进展，任务中止", step)
            continue

        t_step = time.time()
        step_shot = shot  # 本步云端规划所用的截图，供下一步判断「屏幕是否没变」
        # ── 层2：云端规划（看图 + 任务 + 历史 → 下一步动作）──
        user = PLANNER_USER_TEMPLATE.format(
            task=task, history=history.to_text() or "（无）")
        plan_msg = client.chat(
            CLOUD_ENDPOINT,
            [text_msg("system", PLANNER_SYSTEM),
             image_msg("user", user, screen.resize_img(shot, CLOUD_IMG_MAX_SIDE))],
            temperature=0.0, max_tokens=256,
        )
        t_plan = time.time()
        plan = extract_json(plan_msg.content)
        if not isinstance(plan, dict):
            return _result(False, "云端输出无法解析", step)

        action = plan.get("action")
        target = plan.get("target")
        if action == "done":
            return _result(True, plan.get("reason", "完成"), step)
        if action == "fail":
            return _result(False, plan.get("reason", "无法完成"), step)
        if action == "query":
            ans = system_query.try_answer(target or "")
            if ans is None:
                return _result(False, f"无法执行查询: {target}", step)
            return _result(True, ans, step)

        # ── 层3：执行（仅 tap/type 惰性抓 UI 树 + grounding）──
        status, xml_before = _execute(action, plan, target, task, shot, client, executor)
        if status == "unknown":
            return _result(False, f"未知动作类型: {action}", step)
        t_exec = time.time()

        # ── 校验：等 UI 稳定后对比页面指纹 ──────────────
        time.sleep(ACTION_SETTLE_SEC)
        if xml_before is not None:
            # 回退路径：有 UI 树，用树指纹比对
            changed = screen.signature(adb.dump_ui()) != screen.signature(xml_before)
            shot = adb.screencap_fast()  # 下一步截图
        else:
            # 快路径：无 UI 树，抓下一步截图顺带用截图指纹判断页面是否变化
            new_shot = adb.screencap_fast()
            changed = screen.img_changed(shot, new_shot)
            shot = new_shot
        t_verify = time.time()
        prev_shot = step_shot

        history.add(str(action), target or "", changed)

        if verbose:
            print(f"[step {step:02d}] {action} {target or ''} "
                  f"→ {'页面变化' if changed else '页面无变化'} ({status}) "
                  f"| 云端{t_plan-t_step:.1f}s 执行{t_exec-t_plan:.1f}s "
                  f"校验{t_verify-t_exec:.1f}s 合计{t_verify-t_step:.1f}s")

        if router.should_abort(history):
            return _result(False, f"连续 {router.stuck_threshold} 步无进展，任务中止", step)

    return _result(False, "达到最大步数上限", MAX_STEPS)


def _execute(action: str, plan: dict, target: Optional[str], task: str,
             shot: bytes, client: LLMClient,
             executor: ActionExecutor):
    """执行单个动作，返回 (status, xml_before)。

    status: ok / no_target / bad_coord / bad_action / unknown
    xml_before: 动作前抓的 UI 树（仅 tap/type 需要，用于动作后校验）；
                其余动作返回 None，由上层视为确定性变化，省一次抓树。
    """
    if action in ("tap", "type"):
        # 快路径：云端直接给归一化坐标，省掉 dump_ui + 本地 grounding。
        coord = _cloud_coord(plan, executor)
        if coord is not None:
            if action == "tap":
                ok = executor.tap(*coord)
            else:
                ok = executor.type_coord(*coord, plan.get("text", ""))
            return ("ok" if ok else "bad_coord"), None
        # 回退：云端没给有效坐标时，走原来的 UI 树 grounding。
        # 精简 prompt 后 tap/type 不再带 target，这里用任务描述兜底。
        xml = adb.dump_ui()
        elements = screen.parse_ui(xml)
        coord = _locate(target or task, elements, shot, client, executor)
        if coord is None:
            return "no_target", None
        if action == "tap":
            ok = executor.tap(*coord)
        else:
            ok = executor.type_coord(*coord, plan.get("text", ""))
        return ("ok" if ok else "bad_coord"), xml

    if action == "swipe":
        return ("ok" if executor.swipe(plan.get("direction", "up")) else "bad_action"), None

    if action == "back":
        executor.back()
        return "ok", None

    if action == "home":
        executor.home()
        return "ok", None

    if action == "launch":
        pkg = plan.get("package", "")
        if not pkg:
            return "bad_action", None
        executor.launch(pkg)
        return "ok", None

    return "unknown", None


def _locate(target: Optional[str], elements: list[UIElement], shot: bytes,
            client: LLMClient, executor: ActionExecutor):
    """定位目标控件中心坐标：优先 UI 树文本 grounding（快），失败再视觉 grounding（截图）。"""
    el = ground(client, target or "", elements)
    if el is not None:
        return el.center
    if shot:
        return ground_visual(client, target or "", shot, executor._w, executor._h)
    return None


def _cloud_coord(plan: dict, executor: ActionExecutor) -> Optional[tuple[int, int]]:
    """从云端 JSON 里取归一化坐标（0~1000）并换算成像素；缺失/越界返回 None。"""
    try:
        nx = float(plan.get("x"))
        ny = float(plan.get("y"))
    except (TypeError, ValueError):
        return None
    if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
        return None
    return int(nx / 1000 * executor._w), int(ny / 1000 * executor._h)


def _result(success: bool, reason: str, steps: int) -> dict:
    return {"success": success, "reason": reason, "steps": steps}
