"""AndroidLab Clock（闹钟）最小评估：真机跑，adb 判成功。

任务来源：Android-Lab/evaluation/config/clock.yaml 中可映射到本机 com.android.deskclock
的闹钟类任务（闹钟设置/关闭/删除/改重复/查询）。桌面时钟自带的世界时钟/计时器/就寝
任务（clock_15~24）本机 UI 差异大、且无稳定 adb 信号，暂不收录。

判分口径与 Settings 一致：setup 把状态重置到「非目标态」，verify 用 content provider /
sqlite3 读最终状态判「after == 目标」。操作类任务判数据库，查询类任务判 agent 答案。
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Optional

from agent.loop import run_task
from agent.device import app_data

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _reset() -> None:
    app_data.alarm_clear()


def _at(h: int, m: int = 0) -> Optional[dict]:
    for a in app_data.alarm_list():
        if int(a["hour"]) == h and int(a["minutes"]) == m:
            return a
    return None


def _has(h: int, m: int = 0, **fields: int) -> bool:
    a = _at(h, m)
    if a is None:
        return False
    return all(int(a.get(k, -1)) == v for k, v in fields.items())


# (task_id, 中文指令, setup, verify)。verify(r) -> bool。
TASKS: list[tuple[str, str, Callable[[], None], Callable[[dict], bool]]] = [
    ("clock_1", "设置一个下午3点的闹钟，标签为meeting",
     lambda: _reset(),
     lambda r: _has(15, 0, enabled=1) and _at(15, 0)["label"] == "meeting"),
    ("clock_2", "设置一个早上6点45分的闹钟，关闭振动",
     lambda: _reset(),
     lambda r: _has(6, 45, enabled=1, vibrate=0)),
    ("clock_3", "帮我设置一个周一到周五早上7点的闹钟",
     lambda: _reset(),
     lambda r: _has(7, 0, enabled=1, daysofweek=62)),
    ("clock_4", "把9点的闹钟改成每天响",
     lambda: (_reset(), app_data.alarm_add(9, 0, daysofweek=62)),
     lambda r: _has(9, 0, daysofweek=127)),
    ("clock_5", "帮我设置一个明天上午10点30分的闹钟",
     lambda: _reset(),
     lambda r: _has(10, 30, enabled=1)),
    ("clock_6", "设置一个周末晚上10点30分的闹钟，标签为看球赛",
     lambda: _reset(),
     lambda r: _has(22, 30, enabled=1, daysofweek=65) and _at(22, 30)["label"] == "看球赛"),
    ("clock_7", "关闭所有闹钟",
     lambda: (_reset(), app_data.alarm_add(8, 0), app_data.alarm_add(20, 0)),
     lambda r: all(a["enabled"] == "0" for a in app_data.alarm_list())
               and len(app_data.alarm_list()) >= 2),
    ("clock_8", "删除下午2点之后的所有闹钟",
     lambda: (_reset(), app_data.alarm_add(13, 0), app_data.alarm_add(15, 0)),
     lambda r: _at(13, 0) is not None and _at(15, 0) is None),
    ("clock_9", "关闭下午4点的闹钟",
     lambda: (_reset(), app_data.alarm_add(16, 0)),
     lambda r: _has(16, 0, enabled=0)),
    ("clock_26", "关闭早上7点30分的闹钟",
     lambda: (_reset(), app_data.alarm_add(7, 30)),
     lambda r: _has(7, 30, enabled=0)),
    ("clock_27", "设置一个下午3点的闹钟",
     lambda: _reset(),
     lambda r: _has(15, 0, enabled=1)),

    # 查询类：verify 判 agent 答案
    ("clock_10", "我最早开着的闹钟是几点",
     lambda: (_reset(), app_data.alarm_add(9, 0), app_data.alarm_add(6, 0)),
     lambda r: "06:00" in r["reason"]),
    ("clock_11", "下午4点每天都有闹钟吗",
     lambda: (_reset(), app_data.alarm_add(16, 0, daysofweek=127)),
     lambda r: "有闹钟" in r["reason"]),
    ("clock_12", "下午4点的闹钟开振动了吗",
     lambda: (_reset(), app_data.alarm_add(16, 0, vibrate=1)),
     lambda r: "开振动" in r["reason"]),
    ("clock_13", "开着几个闹钟",
     lambda: (_reset(),
              app_data.alarm_add(7, 0), app_data.alarm_add(8, 0),
              app_data.alarm_add(9, 0), app_data.alarm_add(10, 0, enabled=0)),
     lambda r: "3" in r["reason"]),
    ("clock_14", "9点的闹钟开了吗",
     lambda: (_reset(), app_data.alarm_add(9, 0)),
     lambda r: "开着" in r["reason"]),
]


def main() -> None:
    _reset()
    print(f"{'task_id':<10} {'指令':<26} {'步':>2} {'耗时':>7} {'判定'}")
    print("-" * 68)
    ok = 0
    total = 0
    t_total = 0.0
    try:
        for tid, task, setup, verify in TASKS:
            adb_home()
            time.sleep(0.2)
            setup()
            time.sleep(0.2)
            t0 = time.time()
            r = run_task(task, verbose=False)
            dt = time.time() - t0
            t_total += dt
            total += 1
            passed = bool(verify(r))
            ok += passed
            print(f"{tid:<10} {task:<26} {r['steps']:>2} {dt:>6.2f}s {'✅' if passed else '❌'}")
    finally:
        _reset()

    print("-" * 68)
    print(f"闹钟任务成功率: {ok}/{total} = {ok / total * 100:.0f}%")
    print(f"平均单步耗时: {t_total / max(total, 1):.2f}s")


def adb_home() -> None:
    from agent.device import adb
    adb.run_text("shell", "input", "keyevent", "3")


if __name__ == "__main__":
    main()
