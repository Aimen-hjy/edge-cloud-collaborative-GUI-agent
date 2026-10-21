"""端侧系统操作：本地 2B 模型把自然语言任务路由到安全的 adb 系统命令。

与 device/system_query.py（关键词快路径）的差别：这里用模型做语义路由，
能处理「关闭 WiFi」「调亮一点」「静音」这类关键词表接不住的变化说法。

安全设计：模型只能从 CAPABILITIES 表挑选能力 + 参数，实际命令硬编码在
各执行器里，杜绝模型幻觉执行任意 shell；模型判断需要看屏幕时输出
escalate，由上层升级到云端 GUI 规划。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from ..config import LOCAL_ENDPOINT
from ..device import adb
from ..llm.client import LLMClient, extract_json, text_msg
from ..llm.prompts import SYSTEM_ROUTER_SYSTEM, SYSTEM_ROUTER_USER


@dataclass
class Capability:
    name: str
    desc: str                        # 给模型看的自然语言说明
    params: str                      # 参数说明（给模型）
    run: Callable[[dict], str]       # 执行器，接收 params dict，返回人话结果


# ── 执行器 ───────────────────────────────────────────
def _query_battery(_p: dict) -> str:
    m = re.search(r"level:\s*(\d+)", adb.run_text("shell", "dumpsys", "battery"))
    return f"当前电量 {m.group(1)}%" if m else "无法获取电量"


def _query_time(_p: dict) -> str:
    return f"当前时间 {adb.run_text('shell', 'date').strip()}"


def _query_storage(_p: dict) -> str:
    out = adb.run_text("shell", "df", "-k", "/data")
    lines = [l for l in out.strip().splitlines() if l.strip()]
    fields = lines[-1].split() if lines else []
    if len(fields) < 4:
        return "无法获取存储"
    return f"/data 可用空间 {int(fields[3]) / 1024 / 1024:.1f} GB"


def _query_memory(_p: dict) -> str:
    m = re.search(r"MemAvailable:\s*(\d+)", adb.run_text("shell", "cat", "/proc/meminfo"))
    return f"可用内存 {int(m.group(1)) / 1024:.0f} MB" if m else "无法获取内存"


def _set_wifi(p: dict) -> str:
    on = bool(p.get("on", False))
    adb.run_text("shell", "svc", "wifi", "enable" if on else "disable")
    return f"WiFi 已{'开启' if on else '关闭'}"


def _set_bluetooth(p: dict) -> str:
    on = bool(p.get("on", False))
    adb.run_text("shell", "svc", "bluetooth", "enable" if on else "disable")
    return f"蓝牙已{'开启' if on else '关闭'}"


def _set_brightness(p: dict) -> str:
    lvl = max(0, min(255, int(p.get("level", 128))))
    adb.run_text("shell", "settings", "put", "system", "screen_brightness", str(lvl))
    return f"亮度已设为 {lvl}"


def _set_volume(p: dict) -> str:
    lvl = max(0, min(15, int(p.get("level", 5))))
    adb.run_text("shell", "media", "volume", "--set", str(lvl))
    return f"媒体音量已设为 {lvl}"


def _set_airplane(p: dict) -> str:
    on = bool(p.get("on", False))
    adb.run_text("shell", "settings", "put", "global", "airplane_mode_on",
                 "1" if on else "0")
    adb.run_text("shell", "am", "broadcast", "-a",
                 "android.intent.action.AIRPLANE_MODE", "--ez", "state",
                 "true" if on else "false")
    return f"飞行模式已{'开启' if on else '关闭'}"


def _lock_screen(_p: dict) -> str:
    adb.run_text("shell", "input", "keyevent", "26")  # POWER
    return "已锁屏"


def _wake_screen(_p: dict) -> str:
    adb.run_text("shell", "input", "keyevent", "224")  # WAKEUP
    return "已亮屏"


# (能力名, 说明, 参数说明, 执行器) —— 只增不改命令，模型只能从这里挑
CAPABILITIES: list[Capability] = [
    Capability("query_battery", "查询当前电量", "无参数", _query_battery),
    Capability("query_time", "查询当前时间", "无参数", _query_time),
    Capability("query_storage", "查询剩余存储空间", "无参数", _query_storage),
    Capability("query_memory", "查询可用内存", "无参数", _query_memory),
    Capability("set_wifi", "开启或关闭 WiFi", "on: true/false", _set_wifi),
    Capability("set_bluetooth", "开启或关闭蓝牙", "on: true/false", _set_bluetooth),
    Capability("set_brightness", "调整屏幕亮度的明暗程度", "level: 0~255 整数", _set_brightness),
    Capability("set_volume", "调整媒体音量", "level: 0~15 整数", _set_volume),
    Capability("set_airplane", "开启或关闭飞行模式", "on: true/false", _set_airplane),
    Capability("lock_screen", "锁屏", "无参数", _lock_screen),
    Capability("wake_screen", "唤醒/点亮屏幕（黑屏变亮）", "无参数", _wake_screen),
]

_BY_NAME = {c.name: c for c in CAPABILITIES}

# 明显的 GUI 线索：出现这些词基本确定要操作屏幕，直接升级云端，省一次模型调用。
# 故意不收「屏幕」「打开」「启动」「进入」等歧义词——「调亮屏幕」是亮度、「打开 WiFi」是开关，
# 这些交给模型判断。
_GUI_CUES = (
    "点击", "滑动", "上滑", "下滑", "左滑", "右滑", "长按", "输入", "打字", "填写",
    "图标", "菜单", "列表", "按钮", "界面", "状态栏", "通知栏", "页面", "设置",
    "浏览器", "微信", "淘宝", "支付宝", "抖音", "京东", "微博", "相机", "相册", "日历",
    "地图", "音乐", "视频", "应用商店", "拨号", "短信", "通讯录",
)


def looks_like_gui(task: str) -> bool:
    return any(c in task for c in _GUI_CUES)


def dispatch(name: Optional[str], params: Optional[dict]) -> Optional[str]:
    """执行能力，返回人话结果；未知能力/非法参数返回 None（上层升级云端）。"""
    cap = _BY_NAME.get(name or "")
    if cap is None:
        return None
    try:
        return cap.run(params or {})
    except (ValueError, TypeError):
        return None


def route_with_model(task: str, client: LLMClient) -> Optional[str]:
    """本地模型路由：任务能用系统命令完成 → 执行并返回结果；需看屏幕/失败 → None（升级云端）。"""
    if not task:
        return None
    if looks_like_gui(task):
        return None  # 明显要操作屏幕，直接升级云端，别让模型硬猜
    tools = "\n".join(f"- {c.name}: {c.desc}（参数：{c.params}）" for c in CAPABILITIES)
    system = SYSTEM_ROUTER_SYSTEM.format(tools=tools)
    user = SYSTEM_ROUTER_USER.format(task=task)
    msg = client.chat(
        LOCAL_ENDPOINT,
        [text_msg("system", system), text_msg("user", user)],
        temperature=0.0, max_tokens=128, prefix_cache=True,
    )
    data = extract_json(msg.content)
    if not isinstance(data, dict) or data.get("escalate"):
        return None
    return dispatch(data.get("capability"), data.get("params") or {})
