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
from datetime import datetime, timedelta, timezone
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


# ── 日历（com.android.providers.calendar，Events/Reminders 表）──────────────────
# dtstart/dtend 为 UTC 毫秒；eventTimezone 存 Asia/Beijing（本机 Local Calendar=1）。
# rrule 存 iCalendar 字符串（FREQ=DAILY/WEEKLY/MONTHLY）。Reminders 表 minutes=提前分钟数。
CAL_DB = "/data/data/com.android.providers.calendar/databases/calendar.db"
CAL_ID = 1  # 本机「Local Calendar」的 calendar_id
_CAL_TZ = timezone(timedelta(hours=8))  # Asia/Beijing，无夏令时
_EVENT_COLS = ["_id", "title", "description", "dtstart", "dtend", "allDay", "rrule", "hasAlarm"]


def _now_device() -> datetime:
    """设备当前时刻（Asia/Beijing），用设备 epoch 秒避免宿主机时钟偏差。"""
    s = int(adb.run_text("shell", "date", "+%s").strip())
    return datetime.fromtimestamp(s, tz=_CAL_TZ)


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def event_list() -> list[dict]:
    out = _sql(CAL_DB, "SELECT " + ", ".join(_EVENT_COLS) +
               " FROM Events WHERE deleted=0 ORDER BY _id;")
    events: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        vals = line.split("|")
        events.append({_EVENT_COLS[i]: (vals[i] if i < len(vals) else "")
                       for i in range(len(_EVENT_COLS))})
    return events


def event_find(title: str) -> Optional[dict]:
    for e in reversed(event_list()):
        if e.get("title") == title:
            return e
    return None


def event_clear() -> None:
    _sql(CAL_DB, "DELETE FROM Events;")
    _sql(CAL_DB, "DELETE FROM Reminders;")


def event_add(title: str, dtstart_ms: int, dtend_ms: int, description: str = "",
              allDay: int = 0, rrule: str = "", reminders: tuple[int, ...] = ()) -> int:
    """sqlite3 直插事件（~0.2s）。有提醒则顺带插 Reminders 并把 hasAlarm=1。"""
    has_alarm = 1 if reminders else 0
    _sql(CAL_DB, f"INSERT INTO Events (calendar_id, title, description, dtstart, dtend, "
                 f"eventTimezone, allDay, rrule, hasAlarm) VALUES "
                 f"({CAL_ID}, {_q(title)}, {_q(description)}, {dtstart_ms}, {dtend_ms}, "
                 f"'Asia/Beijing', {int(allDay)}, {_q(rrule)}, {has_alarm});")
    eid = int(event_find(title)["_id"])
    for m in reminders:
        _sql(CAL_DB, f"INSERT INTO Reminders (event_id, minutes, method) VALUES ({eid}, {int(m)}, 1);")
    return eid


def event_update(title: str, **fields) -> None:
    sets = [f"{k}={_q(v)}" if isinstance(v, str) else f"{k}={int(v)}"
            for k, v in fields.items()]
    if sets:
        _sql(CAL_DB, f"UPDATE Events SET {', '.join(sets)} WHERE title={_q(title)} AND deleted=0;")


def event_reminders(title: str) -> list[int]:
    e = event_find(title)
    if not e:
        return []
    out = _sql(CAL_DB, f"SELECT minutes FROM Reminders WHERE event_id={e['_id']} ORDER BY minutes;")
    return [int(x) for x in out.split() if x.strip().isdigit()]


def reminder_clear(title: str) -> None:
    e = event_find(title)
    if e:
        _sql(CAL_DB, f"DELETE FROM Reminders WHERE event_id={e['_id']};")


def reminder_add(title: str, minutes: int) -> None:
    e = event_find(title)
    if e:
        _sql(CAL_DB, f"INSERT INTO Reminders (event_id, minutes, method) VALUES ({e['_id']}, {int(minutes)}, 1);")
        _sql(CAL_DB, f"UPDATE Events SET hasAlarm=1 WHERE _id={e['_id']};")


def _extract_event_title(text: str) -> str:
    """提取日程标题：优先「标题是/叫/名为 X」，否则取「日程/事件」后紧跟的英文 token。"""
    m = re.search(r"(?:标题|题目|名字|名称|叫|叫作|名为)\s*(?:是|为|：|:)?\s*([A-Za-z0-9][A-Za-z0-9 _-]*)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:日程|事件|event)\s*(?:一(?:个|条|项))?\s*([A-Za-z][A-Za-z0-9 _-]*?)"
                  r"(?=\s*(?:的|添加|改为|改成|设为|设成|调成|调为|结束|提醒|备注|重复|全天|，|,|。|$))", text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return ""


def _parse_event_date(text: str) -> Optional[datetime]:
    now = _now_device()
    if "后天" in text:
        return now + timedelta(days=2)
    if "明天" in text:
        return now + timedelta(days=1)
    if "今天" in text:
        return now
    m = re.search(r"(\d{4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*[日号]?", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=_CAL_TZ)
    m = re.search(r"(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*[日号]?", text)
    if m:
        return datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=_CAL_TZ)
    return None


def _parse_rrule(text: str) -> str:
    if re.search(r"每月|每个月|monthly", text, re.I):
        return "FREQ=MONTHLY"
    if re.search(r"每周|每个星期|weekly", text, re.I):
        return "FREQ=WEEKLY"
    if re.search(r"每天|每日|daily", text, re.I):
        return "FREQ=DAILY"
    return ""


def _parse_reminders(text: str) -> list[int]:
    # 「提前5分钟和10分钟」「提前5、10分钟」：取「提前」之后出现的所有数字，
    # 避免「提前」只写一次、后面的分钟数漏掉。
    m = re.search(r"提前", text)
    if not m:
        return []
    return [int(x) for x in re.findall(r"\d+", text[m.end():])]


def _parse_note(text: str) -> str:
    m = re.search(r"备注\s*(?:是|为|：|:)?\s*(.+?)\s*$", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:note|Note)\s*(.+?)\s*$", text)
    return m.group(1).strip() if m else ""


_FREQ_CN = {"FREQ=DAILY": "每天", "FREQ=WEEKLY": "每周", "FREQ=MONTHLY": "每月"}


def try_calendar_op(text: str) -> Optional[str]:
    """日历操作确定性路由：添加日程 / 改结束时间 / 加备注 / 改提醒 / 改重复 / 设全天。"""
    if not any(w in text for w in ("日程", "日历", "事件", "event")):
        return None
    title = _extract_event_title(text)
    note = _parse_note(text)
    rrule = _parse_rrule(text)
    reminders = _parse_reminders(text)
    is_edit = bool(re.search(r"(把日程|给日程|编辑日程|修改日程|将日程|对日程)", text))

    if is_edit:
        if not title or not event_find(title):
            return f"没有找到日程 {title}"
        # 加备注（先于其它编辑分支：备注文本里可能不含「结束/重复」等词）
        if note and "备注" in text:
            cur = event_find(title)
            desc = ((cur.get("description") or "").strip() + " " + note).strip()
            event_update(title, description=desc)
            return f"已给日程 {title} 添加备注 {note}"
        if "结束" in text:
            tm = parse_time(text)
            if tm:
                h, m = tm
                dtstart = int(event_find(title)["dtstart"])
                d = datetime.fromtimestamp(dtstart / 1000, tz=_CAL_TZ)
                event_update(title, dtend=_to_ms(d.replace(hour=h, minute=m, second=0, microsecond=0)))
                return f"已把日程 {title} 结束时间改为 {h:02d}:{m:02d}"
        if rrule:
            event_update(title, rrule=rrule)
            return f"已把日程 {title} 设为{_FREQ_CN.get(rrule, rrule)}重复"
        if reminders:
            reminder_clear(title)
            for m in reminders:
                reminder_add(title, m)
            return f"已把日程 {title} 提醒改为提前 {'、'.join(map(str, reminders))} 分钟"
        if "全天" in text:
            event_update(title, allDay=1)
            return f"已把日程 {title} 设为全天"
        return None

    # 添加日程
    if not re.search(r"(安排|添加|新建|创建|加一(?:个|条)|帮我.{0,4}(?:安排|创建))", text):
        return None
    d = _parse_event_date(text)
    now = _now_device()
    if d is None:
        d = now
    tm = parse_time(text)
    h, m = tm if tm else (9, 0)
    dtstart = d.replace(hour=h, minute=m, second=0, microsecond=0)
    dtend = dtstart + timedelta(hours=1)
    allDay = 1 if "全天" in text else 0
    if not title:
        title = "新日程"
    event_add(title, _to_ms(dtstart), _to_ms(dtend), description=note, allDay=allDay,
              rrule=rrule, reminders=tuple(reminders))
    msg = f"已添加日程 {title}"
    if note:
        msg += f"，备注 {note}"
    if reminders:
        msg += f"，提前 {reminders[0]} 分钟提醒"
    return msg


# ── 联系人（com.android.providers.contacts，contacts2.db）──────────────────
# 数据模型：raw_contacts(account_id, contact_id, display_name) ↔ contacts(_id,
# name_raw_contact_id) ↔ data(raw_contact_id, mimetype_id, data1..data15)。
# mimetype_id：name=7, email_v2=1, organization=4, phone_v2=5, group_membership=11,
# contact_event=13, website=14。电话 data2 类型 2=手机 3=工作；邮箱 data2 2=工作；
# 事件 data2 3=生日（data1 存 yyyy-MM-dd）。
CONTACTS_DB = "/data/data/com.android.providers.contacts/databases/contacts2.db"
_EVENT_BDAY = 3
_account_id: Optional[int] = None


def _ins_last_id(db: str, sql: str) -> int:
    """执行 INSERT 并在同一 sqlite3 连接里 SELECT last_insert_rowid() 返回新 id。"""
    out = _sql(db, sql + "; SELECT last_insert_rowid();")
    return int(out.strip().splitlines()[-1])


def _ensure_account() -> int:
    global _account_id
    if _account_id is not None:
        return _account_id
    out = _sql(CONTACTS_DB, "SELECT _id FROM accounts WHERE account_type='com.android.local' ORDER BY _id LIMIT 1;")
    if out.strip():
        _account_id = int(out.strip().splitlines()[0])
        return _account_id
    _account_id = _ins_last_id(CONTACTS_DB,
                               "INSERT INTO accounts (account_name, account_type) VALUES ('Local', 'com.android.local')")
    return _account_id


def contact_clear() -> None:
    _sql(CONTACTS_DB, "DELETE FROM data; DELETE FROM raw_contacts; DELETE FROM contacts; DELETE FROM groups;")
    # 保留 accounts（本地账号复用）


def _contact_rid(name: str) -> Optional[int]:
    out = _sql(CONTACTS_DB, f"SELECT _id FROM raw_contacts WHERE display_name={_q(name)} AND deleted=0 "
                           f"ORDER BY _id DESC LIMIT 1;")
    return int(out.strip()) if out.strip() else None


def contact_find(name: str) -> Optional[int]:
    return _contact_rid(name)


def contact_add(name: str, given: str = "", family: str = "", phones: tuple[tuple[str, int], ...] = (),
                emails: tuple[tuple[str, int], ...] = (), org: str = "", website: str = "",
                birthday: str = "") -> int:
    """新增联系人（name + 可选电话/邮箱/公司/网站/生日），返回 raw_contact_id。"""
    aid = _ensure_account()
    cid = _ins_last_id(CONTACTS_DB, f"INSERT INTO contacts (has_phone_number) VALUES ({1 if phones else 0})")
    rid = _ins_last_id(CONTACTS_DB, f"INSERT INTO raw_contacts (account_id, contact_id, display_name, sort_key) "
                                   f"VALUES ({aid}, {cid}, {_q(name)}, {_q(name)})")
    stmts = [f"INSERT INTO data (mimetype_id, raw_contact_id, data1, data2, data3, is_primary) "
             f"VALUES (7, {rid}, {_q(name)}, {_q(given or name)}, {_q(family)}, 1)"]
    for num, ptype in phones:
        stmts.append(f"INSERT INTO data (mimetype_id, raw_contact_id, data1, data2, is_primary) "
                     f"VALUES (5, {rid}, {_q(num)}, {int(ptype)}, 1)")
    for mail, etype in emails:
        stmts.append(f"INSERT INTO data (mimetype_id, raw_contact_id, data1, data2, is_primary) "
                     f"VALUES (1, {rid}, {_q(mail)}, {int(etype)}, 1)")
    if org:
        stmts.append(f"INSERT INTO data (mimetype_id, raw_contact_id, data1) VALUES (4, {rid}, {_q(org)})")
    if website:
        stmts.append(f"INSERT INTO data (mimetype_id, raw_contact_id, data1, data2) VALUES (14, {rid}, {_q(website)}, 1)")
    if birthday:
        stmts.append(f"INSERT INTO data (mimetype_id, raw_contact_id, data1, data2) VALUES (13, {rid}, {_q(birthday)}, {_EVENT_BDAY})")
    stmts.append(f"UPDATE contacts SET name_raw_contact_id={rid} WHERE _id={cid}")
    _sql(CONTACTS_DB, "; ".join(stmts))
    return rid


def contact_delete(name: str) -> None:
    rid = _contact_rid(name)
    if rid:
        _sql(CONTACTS_DB, f"DELETE FROM raw_contacts WHERE _id={rid};")


def contact_phones(name: str) -> list[str]:
    rid = _contact_rid(name)
    if not rid:
        return []
    out = _sql(CONTACTS_DB, f"SELECT data1 FROM data WHERE raw_contact_id={rid} AND mimetype_id=5 ORDER BY _id;")
    return [x for x in out.splitlines() if x.strip()]


def contact_emails(name: str) -> list[str]:
    rid = _contact_rid(name)
    if not rid:
        return []
    out = _sql(CONTACTS_DB, f"SELECT data1 FROM data WHERE raw_contact_id={rid} AND mimetype_id=1 ORDER BY _id;")
    return [x for x in out.splitlines() if x.strip()]


def contact_org(name: str) -> str:
    rid = _contact_rid(name)
    if not rid:
        return ""
    return _sql(CONTACTS_DB, f"SELECT data1 FROM data WHERE raw_contact_id={rid} AND mimetype_id=4 LIMIT 1;").strip()


def contact_website(name: str) -> str:
    rid = _contact_rid(name)
    if not rid:
        return ""
    return _sql(CONTACTS_DB, f"SELECT data1 FROM data WHERE raw_contact_id={rid} AND mimetype_id=14 LIMIT 1;").strip()


def contact_birthday(name: str) -> str:
    rid = _contact_rid(name)
    if not rid:
        return ""
    return _sql(CONTACTS_DB, f"SELECT data1 FROM data WHERE raw_contact_id={rid} AND mimetype_id=13 LIMIT 1;").strip()


def contact_add_phone(name: str, number: str, ptype: int = 2) -> None:
    rid = _contact_rid(name)
    if not rid:
        return
    _sql(CONTACTS_DB, f"INSERT INTO data (mimetype_id, raw_contact_id, data1, data2, is_primary) "
                     f"VALUES (5, {rid}, {_q(number)}, {int(ptype)}, 0);")
    _sql(CONTACTS_DB, f"UPDATE contacts SET has_phone_number=1 WHERE _id=(SELECT contact_id FROM raw_contacts WHERE _id={rid});")


def contact_add_birthday(name: str, birthday: str) -> None:
    rid = _contact_rid(name)
    if rid:
        _sql(CONTACTS_DB, f"INSERT INTO data (mimetype_id, raw_contact_id, data1, data2) "
                         f"VALUES (13, {rid}, {_q(birthday)}, {_EVENT_BDAY});")


def contact_set_website(name: str, url: str) -> None:
    rid = _contact_rid(name)
    if rid:
        _sql(CONTACTS_DB, f"INSERT INTO data (mimetype_id, raw_contact_id, data1, data2) "
                         f"VALUES (14, {rid}, {_q(url)}, 1);")


def group_create(title: str, member_names: list[str]) -> int:
    aid = _ensure_account()
    gid = _ins_last_id(CONTACTS_DB, f"INSERT INTO groups (account_id, title, group_visible) VALUES ({aid}, {_q(title)}, 1)")
    for mn in member_names:
        rid = _contact_rid(mn)
        if rid:
            _sql(CONTACTS_DB, f"INSERT INTO data (mimetype_id, raw_contact_id, data1) VALUES (11, {rid}, {gid});")
    return gid


def group_members(title: str) -> list[str]:
    gid = _sql(CONTACTS_DB, f"SELECT _id FROM groups WHERE title={_q(title)} AND deleted=0 LIMIT 1;").strip()
    if not gid:
        return []
    out = _sql(CONTACTS_DB, f"SELECT r.display_name FROM data d JOIN raw_contacts r ON d.raw_contact_id=r._id "
                           f"WHERE d.mimetype_id=11 AND d.data1={gid};")
    return [x for x in out.splitlines() if x.strip()]


def _extract_contact_name(text: str) -> str:
    m = re.search(r"联系人\s*([A-Za-z一-鿿][A-Za-z0-9一-鿿 ]*?)(?=[，,。；;、的添设改删]|$)", text)
    return m.group(1).strip() if m else ""


def _parse_phones(text: str) -> list[tuple[str, int]]:
    phones: list[tuple[str, int]] = []
    for m in re.finditer(r"(工作电话|工作手机|手机号|手机|电话|电话号码)\s*[:：]?\s*(\d{4,})", text):
        phones.append((m.group(2), 3 if "工作" in m.group(1) else 2))
    return phones


def _parse_emails(text: str) -> list[tuple[str, int]]:
    emails: list[tuple[str, int]] = []
    for m in re.finditer(r"(工作邮箱|邮箱|email|邮件)\s*[:：]?\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", text, re.I):
        emails.append((m.group(2), 2 if "工作" in m.group(1) else 1))
    return emails


def _parse_org(text: str) -> str:
    m = re.search(r"(?:公司|单位|organization|org)\s*(?:是|为|：|:)?\s*([^\s，,。；;、]+(?:\s+[^\s，,。；;、]+)*)", text)
    return m.group(1).strip() if m else ""


def _parse_birthday(text: str) -> str:
    m = re.search(r"生日\s*(?:是|为|：|:)?\s*(\d{4})[年/.\-](\d{1,2})[月/.\-](\d{1,2})[日号]?", text)
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def _parse_website(text: str) -> str:
    m = re.search(r"(?:网站|网址|website|url)\s*(?:设为|设成|改成|改为|是|为|：|:)?\s*([a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z0-9.\-]+)", text)
    return m.group(1) if m else ""


def try_contact_op(text: str) -> Optional[str]:
    """联系人操作确定性路由：新增/删除/加电话/加生日/设网站/建分组。"""
    if not any(w in text for w in ("联系人", "通讯录", "contact", "分组", "标签")):
        return None
    name = _extract_contact_name(text)

    # 新建分组
    if "分组" in text and re.search(r"(新建|创建|添加|建立)", text):
        m = re.search(r"分组\s*([A-Za-z一-鿿0-9]+)", text)
        gname = m.group(1) if m else ""
        members: list[str] = []
        mm = re.search(r"把\s*([^加进]+?)\s*(?:加进去|加入|加进)", text)
        if mm:
            members = [x.strip() for x in re.split(r"[和、及与,，]", mm.group(1)) if x.strip()]
        if gname:
            group_create(gname, members)
            return f"已新建分组 {gname}，成员 {members}"

    # 删除联系人
    if re.search(r"(删除|移除|删掉)", text) and name:
        if _contact_rid(name):
            contact_delete(name)
            return f"已删除联系人 {name}"
        return f"没有找到联系人 {name}"

    # 加生日
    if "生日" in text and re.search(r"(添加|加|设为|改成|改为|设置)", text) and name:
        bday = _parse_birthday(text)
        if _contact_rid(name) and bday:
            contact_add_birthday(name, bday)
            return f"已给 {name} 添加生日 {bday}"

    # 设网站
    if re.search(r"(网站|网址)", text) and name:
        url = _parse_website(text)
        if _contact_rid(name) and url:
            contact_set_website(name, url)
            return f"已把 {name} 的网站设为 {url}"

    # 加电话（编辑：给联系人X添加工作电话Y —— 「添加」离「电话」很近）
    if re.search(r"(添加|加|新增).{0,6}(电话|手机|号码)", text) and name:
        phones = _parse_phones(text)
        if _contact_rid(name) and phones:
            num, ptype = phones[0]
            contact_add_phone(name, num, ptype)
            return f"已给 {name} 添加{'工作' if ptype == 3 else ''}电话 {num}"

    # 新增联系人
    if re.search(r"(添加|新建|创建|加一个|加一)", text) and name:
        phones = _parse_phones(text)
        emails = _parse_emails(text)
        contact_add(name, phones=tuple(phones), emails=tuple(emails),
                    org=_parse_org(text), website=_parse_website(text), birthday=_parse_birthday(text))
        msg = f"已添加联系人 {name}"
        if phones:
            msg += f"，电话 {phones[0][0]}"
        if emails:
            msg += f"，邮箱 {emails[0][0]}"
        return msg
    return None


def try_contact_query(text: str) -> Optional[str]:
    """联系人查询确定性回答：电话号码/邮箱/生日/公司。"""
    if not any(w in text for w in ("联系人", "通讯录", "contact")):
        return None
    name = _extract_contact_name(text)
    if not name:
        return None
    if re.search(r"(电话|手机|号码)", text) and re.search(r"(多少|是什么|是多少|吗)", text):
        phones = contact_phones(name)
        if phones:
            return f"{name} 的电话号码是 {phones[0]}"
        return f"联系人 {name} 没有电话"
    if re.search(r"(邮箱|email|邮件)", text) and re.search(r"(什么|多少|吗)", text):
        emails = contact_emails(name)
        if emails:
            return f"{name} 的邮箱是 {emails[0]}"
        return f"联系人 {name} 没有邮箱"
    if "生日" in text and re.search(r"(什么时候|是什么|哪一天|吗)", text):
        bday = contact_birthday(name)
        return f"{name} 的生日是 {bday}" if bday else f"联系人 {name} 没有生日"
    if re.search(r"(公司|单位)", text) and re.search(r"(什么|哪|吗)", text):
        org = contact_org(name)
        return f"{name} 的公司是 {org}" if org else f"联系人 {name} 没有公司信息"
    return None
