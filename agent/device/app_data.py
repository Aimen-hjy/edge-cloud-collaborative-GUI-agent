"""应用级数据确定性读写：闹钟 / 日历 / 联系人。

这些是「打开 App 才能完成」的任务，但本机已 root，可直接经 sqlite3 读写真机
数据库，做到单步 <1s 且结果确定，不必烧云端 GUI 规划。content 命令每次 ~2s，
sqlite3 只要 ~0.2s，所以这里一律走 sqlite3；provider 是实时读表，sqlite3 写入
后 content query / App 都能看到。

引号约定（adb shell 会把 args 用空格拼起来再交给设备 shell 重新解析）：
  - sqlite3：整条命令作为一个字符串 arg 传入，SQL 用双引号包裹、内部字符串字面量
    用单引号（如 WHERE title='work'）。故 SQL 值里不得出现双引号。

本模块只依赖 adb，不依赖上层路由，避免循环导入。
"""
from __future__ import annotations

import re
from typing import Optional

from . import adb


def _sql(db: str, sql: str) -> str:
    """用 sqlite3（root）执行 SQL（可读可写），返回 stdout。SQL 值用单引号、禁双引号。"""
    return adb.run_text("shell", f'sqlite3 {db} "{sql}"').strip()


def _q(s: str) -> str:
    """把字符串转成 SQL 单引号字面量（内部单引号翻倍转义）。"""
    return "'" + (s or "").replace("'", "''") + "'"


# ── 闹钟（com.android.deskclock，alarm_templates 表）──────────────────
# daysofweek 位掩码：周日=1 … 周六=64；0 = 一次性（无重复）。
ALARM_DB = "/data/user_de/0/com.android.deskclock/databases/alarms.db"
_ALARM_COLS = ["_id", "hour", "minutes", "daysofweek", "enabled", "vibrate",
               "label", "ringtone", "delete_after_use"]

_DOW_CN = {"一": 2, "二": 4, "三": 8, "四": 16, "五": 32, "六": 64, "日": 1, "天": 1}
_PERIOD_PM = ("下午", "傍晚", "晚上", "夜里", "夜间")
_TIME_RE = re.compile(r"(凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|夜间)?"
                      r"\s*(\d{1,2})\s*[点:：时]\s*(?:(\d{1,2})\s*分?)?")


def alarm_list() -> list[dict]:
    """查询全部闹钟，返回 [{_id, hour, minutes, daysofweek, enabled, vibrate, label, ...}]。"""
    out = _sql(ALARM_DB, "SELECT " + ", ".join(_ALARM_COLS) + " FROM alarm_templates;")
    alarms: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        vals = line.split("|")
        alarms.append({_ALARM_COLS[i]: (vals[i] if i < len(vals) else "")
                       for i in range(len(_ALARM_COLS))})
    return alarms


def alarm_add(hour: int, minutes: int = 0, daysofweek: int = 127, enabled: int = 1,
              vibrate: int = 1, label: str = "") -> None:
    """新增闹钟（sqlite3 直插，不触发 deskclock 重排闹钟，~0.2s）。"""
    _sql(ALARM_DB, f"INSERT INTO alarm_templates "
                   f"(hour, minutes, daysofweek, enabled, vibrate, label) "
                   f"VALUES ({hour}, {minutes}, {daysofweek}, {enabled}, {vibrate}, {_q(label)});")


def alarm_update_at(hour: int, minutes: int = 0, **fields) -> None:
    """按小时/分钟更新闹钟字段（int 字段给数字，label 给字符串）。"""
    sets = []
    for k, v in fields.items():
        sets.append(f"{k}={_q(v)}" if isinstance(v, str) else f"{k}={int(v)}")
    if sets:
        _sql(ALARM_DB, f"UPDATE alarm_templates SET {', '.join(sets)} "
                       f"WHERE hour={hour} AND minutes={minutes};")


def alarm_delete_at(hour: int, minutes: int = 0) -> None:
    _sql(ALARM_DB, f"DELETE FROM alarm_templates WHERE hour={hour} AND minutes={minutes};")


def alarm_clear() -> None:
    _sql(ALARM_DB, "DELETE FROM alarm_templates;")


def parse_time(text: str) -> Optional[tuple[int, int]]:
    """解析中文时间「下午3点/早上6点45分/晚上10点30分」→ (24h, 分钟)；失败 None。"""
    m = _TIME_RE.search(text)
    if not m:
        return None
    period, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if period in _PERIOD_PM and hh < 12:
        hh += 12
    elif period == "中午" and hh < 11:
        hh += 12
    return hh % 24, mm


def parse_dow(text: str) -> Optional[int]:
    """解析重复模式 → daysofweek 位掩码；未提及返回 None（由调用方决定默认）。"""
    if re.search(r"每天|天天|每天都|每天都响|每天响", text):
        return 127
    if re.search(r"周末|周六.{0,2}周日|周日.{0,2}周六", text):
        return 65
    if re.search(r"工作日|周[一二三四五].{0,2}(?:到|至|和|、).{0,2}周[一二三四五]|"
                 r"星期[一二三四五].{0,2}(?:到|至).{0,2}星期[一二三四五]", text):
        return 62
    days = re.findall(r"周([一二三四五六日天])|星期([一二三四五六日天])", text)
    bits = 0
    for a, b in days:
        bits |= _DOW_CN.get(a or b, 0)
    return bits or None


def _extract_label(text: str) -> str:
    m = re.search(r"(?:标签|名称|备注|叫做|名叫|叫)(?:为|是|：|:)?\s*([^\s，,。；;、]+)", text)
    return m.group(1) if m else ""


def try_alarm_op(text: str) -> Optional[str]:
    """闹钟操作确定性路由：设置/关闭/删除/改重复。命中执行并返回人话；否则 None。"""
    if not any(w in text for w in ("闹钟", "闹铃", "alarm")):
        return None

    # 关闭所有闹钟
    if re.search(r"(关闭|关掉|关|停用|禁用|取消)", text) and re.search(r"(所有|全部|都)", text):
        n = 0
        for a in alarm_list():
            if a.get("enabled") == "1":
                alarm_update_at(int(a["hour"]), int(a["minutes"]), enabled=0)
                n += 1
        return f"已关闭所有闹钟（共 {n} 个）"

    # 删除 X 点之后的闹钟
    if re.search(r"(删除|移除|删掉)", text) and re.search(r"(之后|以后|往后|之后)", text):
        tm = parse_time(text)
        if tm:
            h, mm = tm
            n = 0
            for a in alarm_list():
                ah, am = int(a["hour"]), int(a["minutes"])
                if ah > h or (ah == h and am > mm):
                    alarm_delete_at(ah, am)
                    n += 1
            return f"已删除 {n} 个闹钟"

    # 修改重复：把 X 点的闹钟改成每天/周末/工作日响
    if re.search(r"(改成|改为|设为|变成|调成|调整为)", text) and parse_dow(text):
        tm = parse_time(text)
        dow = parse_dow(text)
        if tm and dow:
            h, mm = tm
            if _at(h, mm):
                alarm_update_at(h, mm, daysofweek=dow)
                return f"已把 {h:02d}:{mm:02d} 的闹钟改为每天响" if dow == 127 else \
                       f"已修改 {h:02d}:{mm:02d} 闹钟的重复方式"

    # 关闭/停用 X 点的闹钟
    if re.search(r"(关闭|关掉|关|停用|禁用)", text):
        tm = parse_time(text)
        if tm:
            h, mm = tm
            if _at(h, mm):
                alarm_update_at(h, mm, enabled=0)
                return f"已关闭 {h:02d}:{mm:02d} 的闹钟"

    # 设置/添加闹钟
    if re.search(r"(设置|设|定|新建|添加|创建|帮我.{0,4}(?:设|定))", text):
        tm = parse_time(text)
        if tm:
            h, mm = tm
            label = _extract_label(text)
            dow = parse_dow(text)
            if dow is None:
                dow = 0 if re.search(r"(明天|今天|后天|明日)", text) else 127
            vibrate = 0 if re.search(r"(关闭振动|关振动|不振动|不要振动|取消振动|振动关)", text) else 1
            alarm_add(h, mm, daysofweek=dow, enabled=1, vibrate=vibrate, label=label)
            return f"已设置 {h:02d}:{mm:02d} 的闹钟" + (f"（标签 {label}）" if label else "")
    return None


def _at(h: int, m: int = 0) -> Optional[dict]:
    for a in alarm_list():
        if int(a["hour"]) == h and int(a["minutes"]) == m:
            return a
    return None


def try_alarm_query(text: str) -> Optional[str]:
    """闹钟查询确定性回答；命中返回答案，否则 None。"""
    if not any(w in text for w in ("闹钟", "闹铃", "alarm")):
        return None
    alarms = alarm_list()

    # 最早开着的闹钟是几点
    if re.search(r"(最早|第一个|最先)", text):
        enabled = [a for a in alarms if a.get("enabled") == "1"]
        if not enabled:
            return "没有开着的闹钟"
        earliest = min(enabled, key=lambda a: int(a["hour"]) * 60 + int(a["minutes"]))
        return f"最早开着的闹钟是 {int(earliest['hour']):02d}:{int(earliest['minutes']):02d}"

    # 开着几个闹钟
    if re.search(r"(几个|多少个|多少)", text):
        n = sum(1 for a in alarms if a.get("enabled") == "1")
        return f"当前开着 {n} 个闹钟"

    # X 点的闹钟开振动了吗（「关闭振动」是操作不是查询，靠「吗」限定为疑问句）
    if re.search(r"(振动|震动)", text) and "吗" in text:
        tm = parse_time(text)
        if tm:
            h, mm = tm
            a = _at(h, mm)
            if a:
                return f"{h:02d}:{mm:02d} 的闹钟{'开' if a.get('vibrate') == '1' else '没开'}振动"
            return f"{h:02d}:{mm:02d} 没有闹钟"

    # X 点的闹钟开了吗
    if re.search(r"(开了吗|开没开|是否开着|开着吗|开启了吗|打开了吗|开了没)", text):
        tm = parse_time(text)
        if tm:
            h, mm = tm
            a = _at(h, mm)
            if a:
                return f"{h:02d}:{mm:02d} 的闹钟{'开着' if a.get('enabled') == '1' else '关着'}"
            return f"{h:02d}:{mm:02d} 没有闹钟"

    # X 点（每天）有闹钟吗（「所有」也含「有」，靠「吗/有没有/是否有」限定为疑问句）
    if re.search(r"(有没有|是否有)", text) or ("吗" in text and re.search(r"(有|存在)", text)):
        tm = parse_time(text)
        if tm:
            h, mm = tm
            want_daily = "每天" in text or "天天" in text
            for a in alarms:
                if int(a["hour"]) == h and int(a["minutes"]) == mm:
                    if want_daily and int(a.get("daysofweek", "0")) != 127:
                        continue
                    return f"{h:02d}:{mm:02d} 有闹钟"
            return f"{h:02d}:{mm:02d} 没有闹钟"
    return None
