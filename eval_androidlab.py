"""AndroidLab 最小可跑评估（Settings 全 23 项）：真机跑，adb 判成功。

背景：AndroidLab 官方 harness 依赖 AVD(Mac)/Docker(Linux)，Windows 跑不了；其 judge
大量匹配 AOSP 英文 UI 字符串，中文 Freeme 平板无法复现。这里退化为「真机端到端 + 等价
adb 命令判最终状态」。

任务来源：Android-Lab/evaluation/config/setting.yaml。每个任务给出：
  - 中文指令（英文原句翻译，用于命中本地中文词表）
  - setup：跑测前把状态重置到「非目标态」，逼 agent 真做动作（消「本来就达标」的假阳性）
  - before：跑测前状态快照
  - verify：等价 adb 判定。verify(r, before) -> bool；None 表示无可靠 adb 信号，只看 agent 自报。
  - 持久/破坏性任务 run 前跳过（改蓝牙名 / 改系统时间 / 加系统语言）。

判定口径：状态型任务「before != 目标 && after == 目标」才算成功。
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Optional

from agent.loop import run_task
from agent.device import adb

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _sh(*a: str) -> str:
    return adb.run_text(*a).strip()


# 持久/破坏性任务：改蓝牙名 / 改系统时间 / 加系统语言，跳过。
SKIP = ("setting_4", "setting_12", "setting_15")


def _mut(table: str, key: str, target: str, reset: str):
    """状态型任务：setup 重置到非目标态，before/after 对比判定。"""
    def setup() -> None:
        adb.run_text("shell", "settings", "put", table, key, reset)

    def before() -> str:
        return _sh("shell", "settings", "get", table, key)

    def verify(_r, b) -> bool:
        return b != target and _sh("shell", "settings", "get", table, key) == target

    return setup, before, verify


def _notif_off() -> bool:
    """通讯录通知是否被关闭。cmd appops get 会同时返回「Uid mode」和「包级」两行，
    Uid mode 行永远含 ignore，会误导判定；只看包级 POST_NOTIFICATION 行。"""
    out = _sh("shell", "cmd", "appops", "get", "com.android.contacts", "POST_NOTIFICATION")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("POST_NOTIFICATION:") and line.split(":", 1)[1].strip() in ("ignore", "deny"):
            return True
    return False


# (task_id, 中文指令, setup, before, verify)。
TASKS: list[tuple[str, str, Optional[Callable[[], None]], Optional[Callable[[], str]],
                  Optional[Callable[[dict, Optional[str]], Optional[bool]]]]] = [
    ("setting_0", "打开飞行模式", *_mut("global", "airplane_mode_on", "1", "0")),
    ("setting_1", "关闭WiFi自动开启", *_mut("global", "wifi_wakeup_enabled", "0", "1")),
    ("setting_2", "把私有DNS设为dns.google",
     (lambda: (adb.run_text("shell", "settings", "put", "global", "private_dns_mode", "off"),
               adb.run_text("shell", "settings", "put", "global", "private_dns_specifier", ""))),
     lambda: _sh("shell", "settings", "get", "global", "private_dns_specifier"),
     lambda r, b: b != "dns.google" and _sh("shell", "settings", "get", "global", "private_dns_specifier") == "dns.google"),
    ("setting_3", "关闭蓝牙",
     (lambda: adb.run_text("shell", "svc", "bluetooth", "enable")),
     lambda: _sh("shell", "settings", "get", "global", "bluetooth_on"),
     lambda r, b: b == "1" and _sh("shell", "settings", "get", "global", "bluetooth_on") != "1"),
    ("setting_4", "把蓝牙设备名改成my AVD", None, None, None),
    ("setting_5", "在状态栏显示电量百分比", *_mut("system", "status_bar_show_battery_percent", "1", "0")),
    ("setting_6", "应用占用了多少存储", None, None, None),
    ("setting_7", "打开深色主题", *_mut("secure", "ui_night_mode", "2", "1")),
    ("setting_8", "把亮度调到0%", *_mut("system", "screen_brightness", "0", "128")),
    ("setting_9", "把铃声和通知音量调到0%", *_mut("system", "volume_ring_speaker", "0", "7")),
    ("setting_10", "把闹钟音量调到最大", *_mut("system", "volume_alarm_speaker", "7", "0")),
    ("setting_11", "把文字转语音语言改成中文", None, None, None),
    ("setting_12", "把时间设为2024年5月1日", None, None, None),
    ("setting_13", "关闭铃声振动", *_mut("system", "vibrate_when_ringing", "0", "1")),
    ("setting_14", "我的时区是什么", None, None,
     lambda r, _b: _sh("shell", "getprop", "persist.sys.timezone") in r.get("reason", "")),
    ("setting_15", "添加西班牙语(美国)为第二语言", None, None, None),
    ("setting_16", "手机的主语言是什么", None, None,
     lambda r, _b: _sh("shell", "getprop", "persist.sys.locale") in r.get("reason", "")),
    ("setting_17", "查看Android版本", None, None,
     lambda r, _b: _sh("shell", "getprop", "ro.build.version.release") in r.get("reason", "")),
    ("setting_18", "关闭通讯录的通知",
     (lambda: adb.run_text("shell", "cmd", "appops", "set", "com.android.contacts", "POST_NOTIFICATION", "allow")),
     lambda: "off" if _notif_off() else "on",
     lambda r, b: b == "on" and _notif_off()),
    ("setting_19", "把默认浏览器改成firefox", None, None, None),
    ("setting_20", "卸载booking应用", None, None, None),
    ("setting_21", "打开设置", None, None,
     lambda r, _b: "com.android.settings" in _sh("shell", "dumpsys", "window")),
    ("setting_22", "飞行模式打开了吗", None, None,
     lambda r, _b: (_sh("shell", "settings", "get", "global", "airplane_mode_on") == "1")
     == ("开" in r.get("reason", "") or "open" in r.get("reason", "").lower())),
]


# 跑测前后要抓/还原的设置键（跑测可能改动它们，最后还原回跑测前状态）。
_SNAPSHOT_KEYS = [
    ("global", "airplane_mode_on"),
    ("global", "bluetooth_on"),
    ("global", "wifi_on"),
    ("global", "wifi_wakeup_enabled"),
    ("global", "private_dns_mode"),
    ("global", "private_dns_specifier"),
    ("system", "screen_brightness"),
    ("system", "status_bar_show_battery_percent"),
    ("system", "volume_ring_speaker"),
    ("system", "volume_alarm_speaker"),
    ("system", "vibrate_when_ringing"),
    ("secure", "ui_night_mode"),
]


def _snapshot() -> dict:
    return {f"{t}.{k}": _sh("shell", "settings", "get", t, k) for t, k in _SNAPSHOT_KEYS}


def _restore(before: dict) -> None:
    for t, k in _SNAPSHOT_KEYS:
        val = before.get(f"{t}.{k}")
        if val:
            adb.run_text("shell", "settings", "put", t, k, val)
    adb.run_text("shell", "input", "keyevent", "3")


def main() -> None:
    before = _snapshot()
    print(f"{'task_id':<10} {'指令':<20} {'agent':<6} {'步':>2} {'耗时':>7} {'判定'}")
    print("-" * 78)
    verified_ok = verified_n = 0
    try:
        for tid, task, setup, before_fn, verify in TASKS:
            if tid in SKIP:
                print(f"{tid:<10} {task:<20} {'跳过':<6} {'':>2} {'':>7} ⏭️ 持久状态变更")
                continue
            adb.run_text("shell", "input", "keyevent", "3")  # HOME
            time.sleep(0.6)
            if setup:
                setup()
                time.sleep(0.3)
            b = before_fn() if before_fn else None
            t0 = time.time()
            r = run_task(task, verbose=False)
            dt = time.time() - t0
            if verify is None:
                mark = "—(人工)"
                print(f"{tid:<10} {task:<20} {str(r['success']):<6} {r['steps']:>2} "
                      f"{dt:>6.2f}s {mark}  {r['reason'][:40]}")
                continue
            ok = verify(r, b)
            verified_ok += bool(ok)
            verified_n += 1
            print(f"{tid:<10} {task:<20} {str(r['success']):<6} {r['steps']:>2} "
                  f"{dt:>6.2f}s {'✅' if ok else '❌'}")
    finally:
        _restore(before)

    print("-" * 78)
    print(f"adb 可判定任务成功率: {verified_ok}/{verified_n}")
    print(f"另有 {len(SKIP)} 项跳过（持久状态变更），"
          f"{sum(1 for _, _, _, _, v in TASKS if v is None) - len(SKIP)} 项需人工/环境不满足。")
    print("设备状态已还原到跑测前。")


if __name__ == "__main__":
    main()
