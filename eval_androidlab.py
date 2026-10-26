"""AndroidLab 最小可跑评估：真机跑 AndroidLab 的 Settings 子集，用 adb_query 判成功。

背景：AndroidLab 官方 harness 依赖 AVD(Mac) / Docker(Linux)，Windows 上跑不了；且其
judge 大量依赖 AOSP 英文 UI 字符串，本机是中文 Freeme 平板，UI 层 judge 无法复现。
这里退化为「最小可跑」——取 AndroidLab 的 Settings 任务指令翻译成中文，在真机上用
run_task 端到端执行，再用每个任务等价的一条 adb 命令核对最终状态（成功率 SR）。

任务来源：Android-Lab/evaluation/config/setting.yaml（setting_0 / setting_3 / setting_8）。
"""
from __future__ import annotations

import sys
import time

from agent.loop import run_task
from agent.device import adb

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# (task_id, 中文指令, [adb 判定命令...], 期望状态判定函数)
TASKS = [
    ("setting_0", "打开飞行模式",
     ["shell", "settings", "get", "global", "airplane_mode_on"],
     lambda out: out.strip() == "1"),
    ("setting_3", "关闭蓝牙",
     ["shell", "settings", "get", "global", "bluetooth_on"],
     lambda out: out.strip() != "1"),
    ("setting_8", "把亮度调到0%",
     ["shell", "settings", "get", "system", "screen_brightness"],
     lambda out: out.strip() == "0"),
]


def main() -> None:
    print(f"{'task_id':<10} {'指令':<12} {'agent':<7} {'步数':>3} {'耗时':>7} {'终态':>8} {'SR'}")
    print("-" * 64)
    ok = 0
    for tid, task, cmd, expect in TASKS:
        adb.run_text("shell", "input", "keyevent", "3")  # HOME，回到一致起始态
        time.sleep(0.6)
        t0 = time.time()
        r = run_task(task, verbose=False)
        dt = time.time() - t0
        state = adb.run_text(*cmd).strip()
        sr = expect(state)
        ok += sr
        print(f"{tid:<10} {task:<12} {str(r['success']):<7} {r['steps']:>3} "
              f"{dt:>6.2f}s {state:>8} {'✅' if sr else '❌'}")
    print("-" * 64)
    print(f"AndroidLab Settings 子集成功率: {ok}/{len(TASKS)}")


if __name__ == "__main__":
    main()
