"""浏览器（com.zui.browser）自定义评估：真机跑，sqlite3 判成功。

对齐 AndroidLab 难度：查询类（书签数量/存在性、最近浏览、最近搜索）+ 操作类
（添加/删除书签、清空历史），verify 用 sqlite3 读 lebrowser.db 判「after == 目标」。
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
    app_data.bookmark_clear()
    app_data.history_clear()
    app_data.search_clear()


def _bm(title: str) -> bool:
    return app_data.bookmark_exists(title)


def _bms() -> int:
    return len(app_data.bookmark_list())


def _hist() -> str:
    return app_data.history_latest()


def _hist_count() -> int:
    return app_data.history_count()


# (task_id, 中文指令, setup, verify)。verify(r) -> bool。
TASKS: list[tuple[str, str, Callable[[], None], Callable[[dict], bool]]] = [
    ("browser_1", "我有几个书签？",
     lambda: (_reset(), app_data.bookmark_add("百度", "http://m.baidu.com/"),
              app_data.bookmark_add("新浪", "http://sina.cn/"),
              app_data.bookmark_add("网易", "http://3g.163.com/")),
     lambda r: "3" in r["reason"]),
    ("browser_2", "我的浏览历史里最近访问的是哪个网页？",
     lambda: (_reset(), app_data.history_add("知乎", "https://www.zhihu.com", 2000),
              app_data.history_add("新浪", "http://sina.cn/", 1000)),
     lambda r: "知乎" in r["reason"]),
    ("browser_3", "我的搜索记录里最近搜的是什么？",
     lambda: (_reset(), app_data.search_add("天气预报", 2000),
              app_data.search_add("新闻", 1000)),
     lambda r: "天气预报" in r["reason"]),
    ("browser_4", "我的书签里有新浪吗？",
     lambda: (_reset(), app_data.bookmark_add("新浪", "http://sina.cn/")),
     lambda r: "里有" in r["reason"]),
    ("browser_5", "添加书签，标题是知乎，网址是https://www.zhihu.com",
     lambda: _reset(),
     lambda r: _bm("知乎")),
    ("browser_6", "删除书签新浪",
     lambda: (_reset(), app_data.bookmark_add("新浪", "http://sina.cn/")),
     lambda r: not _bm("新浪")),
    ("browser_7", "清空浏览历史",
     lambda: (_reset(), app_data.history_add("知乎", "https://www.zhihu.com", 2000),
              app_data.history_add("新浪", "http://sina.cn/", 1000)),
     lambda r: _hist_count() == 0),
    ("browser_8", "添加书签，标题是哔哩哔哩，网址是https://www.bilibili.com",
     lambda: _reset(),
     lambda r: _bm("哔哩哔哩")),
]


def main() -> None:
    _reset()
    print(f"{'task_id':<11} {'指令':<34} {'步':>2} {'耗时':>7} {'判定'}")
    print("-" * 70)
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
            print(f"{tid:<11} {task:<34} {r['steps']:>2} {dt:>6.2f}s {'✅' if passed else '❌'}")
    finally:
        _reset()

    print("-" * 70)
    print(f"浏览器任务成功率: {ok}/{total} = {ok / total * 100:.0f}%")
    print(f"平均单步耗时: {t_total / max(total, 1):.2f}s")


if __name__ == "__main__":
    main()
