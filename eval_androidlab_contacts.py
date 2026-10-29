"""AndroidLab Contacts（联系人）最小评估：真机跑，sqlite3 判成功。

任务来源：Android-Lab/evaluation/config/contacts.yaml。原判分抓 Google Contacts 的
UI 树文本（含美式电话格式化），本机是中文原生联系人（com.android.providers.contacts），
UI 差异大，故沿用等价口径：中文指令映射到同义操作，verify 用 sqlite3 读 contacts2.db
的 data 表判「after == 目标」。电话/邮箱/生日/公司/网站/分组均存 data 表。

不收录：contacts_9（编辑短信草稿，无稳定 DB 信号）、contacts_10（拨号，副作用强）。
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
    app_data.contact_clear()


def _has(name: str) -> bool:
    return app_data.contact_find(name) is not None


def _phones(name: str) -> list[str]:
    return app_data.contact_phones(name)


def _emails(name: str) -> list[str]:
    return app_data.contact_emails(name)


def _org(name: str) -> str:
    return app_data.contact_org(name)


def _bday(name: str) -> str:
    return app_data.contact_birthday(name)


def _web(name: str) -> str:
    return app_data.contact_website(name)


def _members(grp: str) -> list[str]:
    return app_data.group_members(grp)


# (task_id, 中文指令, setup, verify)。verify(r) -> bool。
TASKS: list[tuple[str, str, Callable[[], None], Callable[[dict], bool]]] = [
    ("contacts_1", "添加联系人John，手机号12345678",
     lambda: _reset(),
     lambda r: _has("John") and "12345678" in _phones("John")),
    ("contacts_2", "添加联系人John Smith，手机12345678，工作邮箱123456@gmail.com",
     lambda: _reset(),
     lambda r: _has("John Smith") and "12345678" in _phones("John Smith")
               and "123456@gmail.com" in _emails("John Smith")),
    ("contacts_3", "添加联系人Xu，工作电话12345678，手机87654321",
     lambda: _reset(),
     lambda r: "12345678" in _phones("Xu") and "87654321" in _phones("Xu")),
    ("contacts_4", "添加联系人Chen，公司Tsinghua University",
     lambda: _reset(),
     lambda r: _org("Chen") == "Tsinghua University"),
    ("contacts_5", "新建分组work，把AAA和ABC加进去",
     lambda: (_reset(), app_data.contact_add("AAA"), app_data.contact_add("ABC")),
     lambda r: "AAA" in _members("work") and "ABC" in _members("work")),
    ("contacts_6", "给联系人ABC添加工作电话00112233",
     lambda: (_reset(), app_data.contact_add("ABC")),
     lambda r: "00112233" in _phones("ABC")),
    ("contacts_7", "给联系人AAA添加生日1996年10月24日",
     lambda: (_reset(), app_data.contact_add("AAA")),
     lambda r: _bday("AAA") == "1996-10-24"),
    ("contacts_8", "把联系人ABC的网站设为abc.github.com",
     lambda: (_reset(), app_data.contact_add("ABC")),
     lambda r: _web("ABC") == "abc.github.com"),
    ("contacts_11", "删除联系人AAA",
     lambda: (_reset(), app_data.contact_add("AAA")),
     lambda r: not _has("AAA")),
    ("contacts_12", "联系人ABC的电话号码是多少",
     lambda: (_reset(), app_data.contact_add("ABC", phones=(("12345678", 2),))),
     lambda r: "12345678" in r["reason"]),
    ("contacts_13", "联系人Li的工作邮箱是什么",
     lambda: (_reset(), app_data.contact_add("Li", emails=(("22334455@gmail.com", 2),))),
     lambda r: "22334455@gmail.com" in r["reason"]),
    ("contacts_14", "联系人ABC的生日是什么时候",
     lambda: (_reset(), app_data.contact_add("ABC", birthday="1996-10-24")),
     lambda r: "1996-10-24" in r["reason"]),
    ("contacts_15", "联系人AAA的公司是什么",
     lambda: (_reset(), app_data.contact_add("AAA", org="Tsinghua University")),
     lambda r: "Tsinghua" in r["reason"]),
    # ── 难题：改电话 / 电话计数 / 删电话，当前确定性路由覆盖不到 ──
    ("contacts_16", "把联系人John的手机号改成99999999",
     lambda: (_reset(), app_data.contact_add("John", phones=(("12345678", 2),))),
     lambda r: "99999999" in _phones("John") and "12345678" not in _phones("John")),
    ("contacts_17", "联系人John有几个电话号码",
     lambda: (_reset(), app_data.contact_add("John", phones=(("12345678", 2), ("87654321", 2)))),
     lambda r: "2" in r["reason"]),
    ("contacts_18", "删除联系人John的工作电话",
     lambda: (_reset(), app_data.contact_add("John", phones=(("12345678", 3),))),
     lambda r: _has("John") and len(_phones("John")) == 0),
]


def main() -> None:
    _reset()
    print(f"{'task_id':<12} {'指令':<36} {'步':>2} {'耗时':>7} {'判定'}")
    print("-" * 74)
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
            print(f"{tid:<12} {task:<36} {r['steps']:>2} {dt:>6.2f}s {'✅' if passed else '❌'}")
    finally:
        _reset()

    print("-" * 74)
    print(f"联系人任务成功率: {ok}/{total} = {ok / total * 100:.0f}%")
    print(f"平均单步耗时: {t_total / max(total, 1):.2f}s")


if __name__ == "__main__":
    main()
