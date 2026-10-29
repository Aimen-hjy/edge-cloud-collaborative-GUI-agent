"""系统状态查询：用 dumpsys/shell 直接回答，不经过模型、不抓 UI 树。

大量「看电量 / 几点 / 剩多少空间」这类任务，一条 shell 命令就能答，
走这里 <1s 秒回，省掉云端规划 + 端侧 grounding 两条链路。

只收录命令稳定、解析简单的查询；拿不准的宁可漏掉走正常流程，也不要答错。
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from . import adb


def _parse_battery(out: str) -> str:
    m = re.search(r"level:\s*(\d+)", out)
    return f"当前电量 {m.group(1)}%" if m else "无法获取电量"


def _parse_time(out: str) -> str:
    s = out.strip()
    return f"当前时间 {s}" if s else "无法获取时间"


def _parse_storage(out: str) -> str:
    """df -k /data 的最后一行第 4 列是可用空间（KB）。"""
    lines = [l for l in out.strip().splitlines() if l.strip()]
    fields = lines[-1].split() if lines else []
    if len(fields) < 4:
        return "无法获取存储"
    kb = int(fields[3])
    return f"/data 可用空间 {kb / 1024 / 1024:.1f} GB"


def _parse_memory(out: str) -> str:
    m = re.search(r"MemAvailable:\s*(\d+)", out)
    return f"可用内存 {int(m.group(1)) / 1024:.0f} MB" if m else "无法获取内存"


def _on_off(on: str) -> str:
    return "开" if on.strip() == "1" else "关"


def _parse_brightness(out: str) -> str:
    s = out.strip()
    return f"当前亮度 {s}" if s.isdigit() else "无法获取亮度"


def _parse_wifi(out: str) -> str:
    return f"WiFi {_on_off(out)}"


def _parse_bluetooth(out: str) -> str:
    return f"蓝牙 {_on_off(out)}"


def _parse_airplane(out: str) -> str:
    return f"飞行模式 {_on_off(out)}"


def _parse_version(out: str) -> str:
    s = out.strip()
    return f"Android 版本 {s}" if s else "无法获取 Android 版本"


def _parse_timezone(out: str) -> str:
    s = out.strip()
    return f"时区 {s}" if s else "无法获取时区"


def _parse_locale(out: str) -> str:
    first = out.strip().split(",")[0].strip() if out.strip() else ""
    return f"主语言 {first}" if first else "无法获取主语言"


# (关键词, adb shell 命令, 输出解析器)
_QUERIES: list[tuple[tuple[str, ...], list[str], Callable[[str], str]]] = [
    (("电量", "电池", "battery"), ["dumpsys", "battery"], _parse_battery),
    (("时间", "几点", "日期"), ["date"], _parse_time),
    (("存储", "空间", "剩余容量"), ["df", "-k", "/data"], _parse_storage),
    (("内存", "ram"), ["cat", "/proc/meminfo"], _parse_memory),
    (("亮度",), ["settings", "get", "system", "screen_brightness"], _parse_brightness),
    (("wifi", "无线"), ["settings", "get", "global", "wifi_on"], _parse_wifi),
    (("蓝牙", "bluetooth"), ["settings", "get", "global", "bluetooth_on"], _parse_bluetooth),
    (("飞行模式", "airplane"), ["settings", "get", "global", "airplane_mode_on"], _parse_airplane),
    (("版本", "系统版本", "android", "version"), ["getprop", "ro.build.version.release"], _parse_version),
    (("时区", "timezone"), ["getprop", "persist.sys.timezone"], _parse_timezone),
    (("主语言", "语言", "language", "locale"), ["getprop", "persist.sys.locale"], _parse_locale),
]

# 出现这些「去操作」动词时，是导航/修改意图而非查询意图，交给云端判断，避免误判。
# 「设为/改成/改为/换成/添加」是明确的修改意图；「状态栏/百分比」是设置 UI 里的显示项，
# 不是查询词（「在状态栏显示电量百分比」是改设置，不是查电量）。
_ACTION_VERBS = ("打开", "启动", "进入", "点击", "设置", "关闭", "开启", "切换", "修改", "调整",
                 "调到", "调成", "调为", "调至", "设为", "改成", "改为", "换成", "添加",
                 "状态栏", "百分比")


def try_answer(text: str) -> Optional[str]:
    """文本命中系统查询表 → 执行 shell 命令并返回人话结果；否则返回 None（走正常流程）。"""
    if not text:
        return None
    # 「飞行模式打开了吗」「WiFi开没开」是状态查询，不是「打开X」动作。
    # 先把「打开了吗/开没开/关了没」这类查询后缀剥掉，剩下设备名再查表。
    m = re.match(r"^(.+?)(?:打开了吗|开了吗|开没开|开启了吗|关闭了吗|关了吗|关没关|"
                 r"是否打开|是否开启|是否关闭|开着吗|关着吗)[？?]?$", text)
    if m:
        text = m.group(1).strip()
    if any(v in text for v in _ACTION_VERBS):
        return None
    low = text.lower()
    # 「应用占用了多少存储」问的是应用占用，不是 /data 剩余可用空间；df 答不上，
    # 别用可用空间冒充，放走让云端/正常流程处理。
    if "存储" in text and "应用" in text:
        return None
    for keywords, command, parse in _QUERIES:
        if any(k.lower() in low for k in keywords):
            out = adb.run_text("shell", *command)
            return parse(out)
    return None
