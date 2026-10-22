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


# 内置任务集：覆盖 层1(查询) / 层1.5(本地关键词+打开App+设置子页) / 层2(模型路由) / 层3-4(云端 GUI)。
# 本地类应当 <0.5s；「打开设置，进入X」是复合任务，本地拉起设置后云端只做 1 步。
DEFAULT_TASKS = [
    # ── 层1 查询（shell/dumpsys，<0.2s）──
    "现在几点了",           # query_time
    "电量还有多少",         # query_battery
    "存储还有多少",         # query_storage
    "内存还剩多少",         # query_memory

    # ── 层1.5 关键词系统开关（<0.4s）──
    "关闭WiFi",            # set_wifi off
    "打开WiFi",            # set_wifi on
    "关闭蓝牙",            # set_bluetooth off
    "打开蓝牙",            # set_bluetooth on
    "把屏幕调亮一点",       # set_brightness
    "把屏幕调暗一点",       # set_brightness
    "音量调大",            # set_volume
    "静音",                # set_volume 0

    # ── 层1.5 设置子页 intent（<0.5s，本版新增）──
    "进入WiFi设置",         # WIFI_SETTINGS
    "进入蓝牙设置",         # BLUETOOTH_SETTINGS
    "打开显示设置",         # DISPLAY_SETTINGS
    "打开声音设置",         # SOUND_SETTINGS
    "进入存储设置",         # INTERNAL_STORAGE_SETTINGS

    # ── 层1.5 打开App（<0.6s）──
    "打开设置",            # com.android.settings
    "打开相机",            # com.android.camera2
    "打开日历",            # com.android.calendar
    "打开计算器",          # com.android.calculator2
    "打开浏览器",          # com.zui.browser

    # ── 层1.5 导航 ──
    "回到桌面",            # home
    "返回上一页",          # back

    # ── 层1.6 复合任务拆解 ──
    "打开设置，进入WiFi",   # launch settings + WIFI_SETTINGS
    "打开设置，进入蓝牙",   # launch settings + BLUETOOTH_SETTINGS

    # ── 层3/4 云端 GUI（可能上云：本地拉起后，剩余需看屏点控件/输入）──
    "打开浏览器，搜索AI",     # 本地拉起浏览器 + 云端点搜索框输入
    "打开相机，拍张照片",     # 本地拉起相机 + 云端点快门
    "打开设置，打开显示",     # 本地拉起设置 + 云端点「显示」

    # ── 日常对话/闲聊型（无本地 GUI 能力，测试越界鲁棒性）──
    "今天天气怎么样",        # 无 GUI 动作，预期 fail 或明确回「无法完成」
    "你好，你是谁",          # 身份类闲聊
    "讲个笑话",             # 闲聊
    "今天星期几",            # 日期类（date 可答，当前查询表未覆盖，看是否漏到云端）
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
