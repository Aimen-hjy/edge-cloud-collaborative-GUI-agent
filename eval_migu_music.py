"""咪咕音乐（cmccwm.mobilemusic）自定义评估：真机跑，sqlite3 判成功。

对齐 AndroidLab 难度：查询类（本地歌曲数/存在性、最近播放、收藏列表）+ 操作类
（加入/删除本地歌曲、收藏/取消收藏），verify 用 sqlite3 读 migu_music_song_new.db 判
「after == 目标」。
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
    app_data.migu_clear()


def _find(name: str) -> Optional[dict]:
    return app_data.migu_local_find(name)


def _n_local() -> int:
    return len(app_data.migu_local_list())


# (task_id, 中文指令, setup, verify)。verify(r) -> bool。
TASKS: list[tuple[str, str, Callable[[], None], Callable[[dict], bool]]] = [
    ("migu_1", "我本地音乐里有几首歌？",
     lambda: (_reset(), app_data.migu_local_add("晴天"),
              app_data.migu_local_add("平凡之路"),
              app_data.migu_local_add("海阔天空")),
     lambda r: "3" in r["reason"]),
    ("migu_2", "我最近播放过哪首歌？",
     lambda: (_reset(), app_data.migu_recent_add("晴天", play_date_ms=2000),
              app_data.migu_recent_add("平凡之路", play_date_ms=1000)),
     lambda r: "晴天" in r["reason"]),
    ("migu_3", "我收藏了哪些歌？",
     lambda: (_reset(), app_data.migu_local_add("晴天", collect=0),
              app_data.migu_local_add("平凡之路", collect=1)),
     lambda r: "平凡之路" in r["reason"]),
    ("migu_4", "我本地音乐里有《晴天》吗？",
     lambda: (_reset(), app_data.migu_local_add("晴天")),
     lambda r: "里有" in r["reason"]),
    ("migu_5", "把歌曲《平凡之路》加入本地音乐",
     lambda: _reset(),
     lambda r: _find("平凡之路") is not None),
    ("migu_6", "收藏歌曲《平凡之路》",
     lambda: (_reset(), app_data.migu_local_add("平凡之路", collect=0)),
     lambda r: _find("平凡之路") is not None and _find("平凡之路")["collect"] == "1"),
    ("migu_7", "取消收藏歌曲《平凡之路》",
     lambda: (_reset(), app_data.migu_local_add("平凡之路", collect=1)),
     lambda r: _find("平凡之路") is not None and _find("平凡之路")["collect"] == "0"),
    ("migu_8", "删除本地歌曲《平凡之路》",
     lambda: (_reset(), app_data.migu_local_add("平凡之路")),
     lambda r: _find("平凡之路") is None),
    # ── 难题：收藏计数 / 改元数据 / 按条件删除 / 存在性 yes-no，当前确定性路由覆盖不到 ──
    ("migu_9", "我收藏了几首歌？",
     lambda: (_reset(), app_data.migu_local_add("晴天", collect=0),
              app_data.migu_local_add("平凡之路", collect=1),
              app_data.migu_local_add("海阔天空", collect=1)),
     lambda r: "2" in r["reason"]),
    ("migu_10", "把歌曲《晴天》的歌手改成周杰伦",
     lambda: (_reset(), app_data.migu_local_add("晴天", singer="")),
     lambda r: _find("晴天") is not None and _find("晴天")["singerName"] == "周杰伦"),
    ("migu_11", "删除我收藏的所有歌曲",
     lambda: (_reset(), app_data.migu_local_add("晴天", collect=0),
              app_data.migu_local_add("平凡之路", collect=1),
              app_data.migu_local_add("海阔天空", collect=1)),
     lambda r: all(s.get("collect") != "1" for s in app_data.migu_local_list())),
    ("migu_12", "我最近播放过《晴天》吗？",
     lambda: (_reset(), app_data.migu_recent_add("平凡之路", play_date_ms=2000),
              app_data.migu_recent_add("海阔天空", play_date_ms=1000)),
     lambda r: "没有" in r["reason"]),
]


def main() -> None:
    _reset()
    print(f"{'task_id':<9} {'指令':<32} {'步':>2} {'耗时':>7} {'判定'}")
    print("-" * 68)
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
            print(f"{tid:<9} {task:<32} {r['steps']:>2} {dt:>6.2f}s {'✅' if passed else '❌'}")
    finally:
        _reset()

    print("-" * 68)
    print(f"咪咕音乐任务成功率: {ok}/{total} = {ok / total * 100:.0f}%")
    print(f"平均单步耗时: {t_total / max(total, 1):.2f}s")


if __name__ == "__main__":
    main()
