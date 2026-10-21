"""基准测试：跑一组任务，统计成功率 + 单步耗时，用于回归对比。

用法（仓库根目录）：
    python bench.py                 # 跑内置任务集
    python bench.py "打开设置" "关闭WiFi"   # 跑指定任务

输出每项任务的 {成功/失败, 步数, 总耗时, 平均单步耗时}，最后汇总成功率与平均单步耗时。
单步耗时 = 总耗时 / 步数（步数=0 的纯查询/路由任务按总耗时计，不计入单步平均）。
"""
from __future__ import annotations

import sys
import time

from agent.loop import run_task
from agent.device import adb

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def reset_home() -> None:
    """回到桌面 + 清后台，保证每个任务从一致的起始状态出发。"""
    adb.run_text("shell", "input", "keyevent", "3")  # HOME
    time.sleep(0.6)


# 内置任务集：覆盖 层1(查询) / 层1.5(本地关键词+打开App) / 层2(模型路由) / 层3-4(云端 GUI)。
# 本地类应当 <0.5s；「打开设置，进入WiFi」是复合任务，本地拉起设置后云端只做 1 步。
DEFAULT_TASKS = [
    "现在几点了",           # 层1 查询
    "电量还有多少",         # 层1 查询
    "关闭WiFi",            # 层1.5 关键词
    "把屏幕调亮一点",       # 层1.5 关键词
    "关闭蓝牙",            # 层1.5 关键词
    "打开设置",            # 层1.5 打开App
    "打开相机",            # 层1.5 打开App
    "回到桌面",            # 层1.5 导航
    "打开设置，进入WiFi",   # 层1.6 复合任务拆解 + 云端 1 步
]


def main() -> None:
    tasks = sys.argv[1:] or DEFAULT_TASKS
    rows = []
    total_steps = 0
    total_time = 0.0
    ok = 0
    print(f"{'任务':<16} {'结果':<6} {'步数':>3} {'总耗时':>7} {'单步':>7}")
    print("-" * 48)
    for task in tasks:
        reset_home()
        t0 = time.time()
        r = run_task(task, verbose=False)
        dt = time.time() - t0
        steps = r.get("steps", 0)
        success = r.get("success", False)
        rows.append((task, success, steps, dt))
        step_avg = dt / steps if steps > 0 else dt
        print(f"{task:<16} {'✅' if success else '❌':<6} {steps:>3} {dt:>6.2f}s {step_avg:>6.2f}s")
        ok += success
        total_steps += steps
        total_time += dt

    n = len(tasks)
    print("-" * 48)
    print(f"成功率: {ok}/{n} = {ok / n * 100:.0f}%")
    if total_steps > 0:
        print(f"平均单步耗时(仅多步任务): {total_time / total_steps:.2f}s")


if __name__ == "__main__":
    main()
