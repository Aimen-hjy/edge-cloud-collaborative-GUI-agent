"""AndroidLab Calendar（日历）最小评估：真机跑，sqlite3 判成功。

任务来源：Android-Lab/evaluation/config/calendar.yaml 的 14 项。原判分是抓 Google
Calendar 的 UI 树文本，但本机是中文原生日历（com.android.providers.calendar），UI 差异大，
故沿用 Settings/Clock 的等价口径：中文指令映射到同义操作，verify 用 sqlite3 读
Events/Reminders 表判「after == 目标」。

判分要点（对应原判分文本）：
  calendar_1  添加 work，今天 17:00            → title=work 且 dtstart 北京时 17 点
  calendar_2  添加 homework，5月21日，提前10分  → title=homework 且 reminder 10
  calendar_3  添加 meeting，5月13日，备注 B202   → title=meeting 且 description 含 B202
  calendar_4  2024/6/1 每月重复                  → rrule 含 MONTHLY 且日期 2024-06-01
  calendar_5  work 结束时间改 19:00             → title=work 且 dtend 北京时 19 点
  calendar_6  homework 加备注 101               → description 含 101
  calendar_7  meeting 提醒改 5 分钟和 10 分钟     → reminders == [5, 10]
  calendar_8  work 加备注 computer              → description 含 computer
  calendar_9  work 每天重复                     → rrule 含 DAILY
  calendar_10 添加 this day                     → title=this day 存在
  calendar_11 this day 每周重复                 → rrule 含 WEEKLY
  calendar_12 Today 加备注 Hello                → description 含 Hello
  calendar_13 添加 exam                         → title=exam 存在
  calendar_14 exam 设全天                       → allDay == 1
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from agent.loop import run_task
from agent.device import app_data

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CN_TZ = timezone(timedelta(hours=8))


def _reset() -> None:
    app_data.event_clear()


def _now() -> datetime:
    from agent.device import adb
    return datetime.fromtimestamp(int(adb.run_text("shell", "date", "+%s").strip()), tz=CN_TZ)


def _add(title: str, h: int = 9, m: int = 0) -> int:
    """setup 用：在「今天 h:m」创建一个 1 小时的事件。"""
    dt = _now().replace(hour=h, minute=m, second=0, microsecond=0)
    return app_data.event_add(title, int(dt.timestamp() * 1000),
                              int((dt + timedelta(hours=1)).timestamp() * 1000))


def _ev(title: str) -> Optional[dict]:
    return app_data.event_find(title)


def _hour(ms: str) -> int:
    return datetime.fromtimestamp(int(ms) / 1000, tz=CN_TZ).hour


def _date(ms: str) -> tuple[int, int, int]:
    d = datetime.fromtimestamp(int(ms) / 1000, tz=CN_TZ)
    return (d.year, d.month, d.day)


def _rem(title: str) -> list[int]:
    return app_data.event_reminders(title)


# (task_id, 中文指令, setup, verify)。verify(r) -> bool。
TASKS: list[tuple[str, str, Callable[[], None], Callable[[dict], bool]]] = [
    ("calendar_1", "添加一个今天下午5点的日程，标题是work",
     lambda: _reset(),
     lambda r: _ev("work") is not None and _hour(_ev("work")["dtstart"]) == 17),
    ("calendar_2", "帮我安排一个5月21日的日程homework，提醒设为提前10分钟",
     lambda: _reset(),
     lambda r: _ev("homework") is not None and _rem("homework") == [10]),
    ("calendar_3", "帮我安排一个5月13日的日程meeting，备注conference room B202",
     lambda: _reset(),
     lambda r: _ev("meeting") is not None and "B202" in (_ev("meeting")["description"] or "")),
    ("calendar_4", "安排一个2024年6月1日开始、每月重复的日程",
     lambda: _reset(),
     lambda r: any("MONTHLY" in (e.get("rrule") or "") and _date(e["dtstart"]) == (2024, 6, 1)
                   for e in app_data.event_list())),
    ("calendar_5", "把日程work的结束时间改成下午7点",
     lambda: (_reset(), _add("work", 17)),
     lambda r: _ev("work") is not None and _hour(_ev("work")["dtend"]) == 19),
    ("calendar_6", "给日程homework添加备注classroom 101",
     lambda: (_reset(), _add("homework")),
     lambda r: _ev("homework") is not None and "101" in (_ev("homework")["description"] or "")),
    ("calendar_7", "把日程meeting的提醒改成提前5分钟和10分钟",
     lambda: (_reset(), _add("meeting")),
     lambda r: _rem("meeting") == [5, 10]),
    ("calendar_8", "给日程work添加备注computer",
     lambda: (_reset(), _add("work", 17)),
     lambda r: _ev("work") is not None and "computer" in (_ev("work")["description"] or "")),
    ("calendar_9", "把日程work设为每天重复",
     lambda: (_reset(), _add("work", 17)),
     lambda r: _ev("work") is not None and "DAILY" in (_ev("work")["rrule"] or "")),
    ("calendar_10", "安排一个日程this day",
     lambda: _reset(),
     lambda r: _ev("this day") is not None),
    ("calendar_11", "把日程this day改为每周重复",
     lambda: (_reset(), _add("this day")),
     lambda r: _ev("this day") is not None and "WEEKLY" in (_ev("this day")["rrule"] or "")),
    ("calendar_12", "给日程Today添加备注Hello",
     lambda: (_reset(), _add("Today")),
     lambda r: _ev("Today") is not None and "Hello" in (_ev("Today")["description"] or "")),
    ("calendar_13", "安排一个日程exam",
     lambda: _reset(),
     lambda r: _ev("exam") is not None),
    ("calendar_14", "把日程exam设为全天",
     lambda: (_reset(), _add("exam")),
     lambda r: _ev("exam") is not None and _ev("exam")["allDay"] == "1"),
]


def main() -> None:
    _reset()
    print(f"{'task_id':<12} {'指令':<32} {'步':>2} {'耗时':>7} {'判定'}")
    print("-" * 72)
    ok = 0
    total = 0
    t_total = 0.0
    try:
        for tid, task, setup, verify in TASKS:
            time.sleep(0.1)
            setup()
            time.sleep(0.1)
            t0 = time.time()
            r = run_task(task, verbose=False)
            dt = time.time() - t0
            t_total += dt
            total += 1
            passed = bool(verify(r))
            ok += passed
            print(f"{tid:<12} {task:<32} {r['steps']:>2} {dt:>6.2f}s {'✅' if passed else '❌'}")
    finally:
        _reset()

    print("-" * 72)
    print(f"日历任务成功率: {ok}/{total} = {ok / total * 100:.0f}%")
    print(f"平均单步耗时: {t_total / max(total, 1):.2f}s")


if __name__ == "__main__":
    main()
