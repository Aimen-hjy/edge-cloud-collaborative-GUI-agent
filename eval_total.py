"""统一总评估：跑全部 6 个 app（设置/闹钟/日历/联系人/浏览器/咪咕音乐）的映射+自定义
测试集，汇总总成功率与总平均单步耗时。

口径：
- 可判定任务 = 有 verify 且非「持久状态变更跳过」的任务（设置里 verify=None 的 4 项
  人工/环境不满足不计入成功率，但仍计入平均单步耗时）。
- 总平均单步 = 所有实际 run_task 的任务总耗时 / 任务数（含失败与人工项）。
"""
from __future__ import annotations

import sys
import time

from agent.loop import run_task
from agent.device import adb

import eval_androidlab as settings
import eval_androidlab_alarm as alarm
import eval_androidlab_calendar as calendar
import eval_androidlab_contacts as contacts
import eval_browser as browser
import eval_migu_music as migu

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# (名称, TASKS[(tid, 指令, setup, verify)], 是否每步先回桌面)
UNIFORM = [
    ("闹钟", alarm.TASKS, True),
    ("日历", calendar.TASKS, False),
    ("联系人", contacts.TASKS, False),
    ("浏览器", browser.TASKS, False),
    ("咪咕音乐", migu.TASKS, False),
]


def _run_uniform(name: str, tasks, home_first: bool):
    ok = n = 0
    dt_sum = 0.0
    n_run = 0
    for tid, task, setup, verify in tasks:
        if home_first:
            adb.run_text("shell", "input", "keyevent", "3")
            time.sleep(0.2)
        setup()
        time.sleep(0.1)
        t0 = time.time()
        r = run_task(task, verbose=False)
        dt = time.time() - t0
        dt_sum += dt
        n_run += 1
        passed = bool(verify(r))
        ok += passed
        n += 1
        print(f"  {tid:<14} {task:<36} {r['steps']:>2} {dt:>6.2f}s {'✅' if passed else '❌'}")
    return ok, n, dt_sum, n_run


def _run_settings():
    ok = n = 0
    dt_sum = 0.0
    n_run = 0
    for tid, task, setup, before_fn, verify in settings.TASKS:
        if tid in settings.SKIP:
            print(f"  {tid:<14} {task:<30} ⏭️ 跳过（持久状态变更）")
            continue
        adb.run_text("shell", "input", "keyevent", "3")
        time.sleep(0.6)
        if setup:
            setup()
            time.sleep(0.3)
        b = before_fn() if before_fn else None
        t0 = time.time()
        r = run_task(task, verbose=False)
        dt = time.time() - t0
        dt_sum += dt
        n_run += 1
        if verify is None:
            print(f"  {tid:<14} {task:<30} {r['steps']:>2} {dt:>6.2f}s —(人工/环境) {r['reason'][:30]}")
            continue
        passed = bool(verify(r, b))
        ok += passed
        n += 1
        print(f"  {tid:<14} {task:<30} {r['steps']:>2} {dt:>6.2f}s {'✅' if passed else '❌'}")
    return ok, n, dt_sum, n_run


def main() -> None:
    total_ok = total_n = 0
    total_dt = 0.0
    total_run = 0

    print("== 设置(Settings) ==")
    snap = settings._snapshot()
    try:
        ok, n, dt, nr = _run_settings()
    finally:
        settings._restore(snap)
    total_ok += ok
    total_n += n
    total_dt += dt
    total_run += nr

    for name, tasks, home_first in UNIFORM:
        print(f"== {name} ==")
        ok, n, dt, nr = _run_uniform(name, tasks, home_first)
        total_ok += ok
        total_n += n
        total_dt += dt
        total_run += nr

    print("=" * 64)
    print(f"总可判定任务成功率: {total_ok}/{total_n} = {total_ok / total_n * 100:.1f}%")
    print(f"总平均单步耗时: {total_dt / max(total_run, 1):.2f}s  （实际执行 {total_run} 个任务）")


if __name__ == "__main__":
    main()
