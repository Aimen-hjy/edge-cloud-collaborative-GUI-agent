"""端侧系统操作：本地 0.6B 模型把自然语言任务路由到安全的 adb 系统命令。

与 device/system_query.py（关键词快路径）的差别：这里用模型做语义路由，
能处理「关闭 WiFi」「调亮一点」「静音」这类关键词表接不住的变化说法。

安全设计：模型只能从 CAPABILITIES 表挑选能力 + 参数，实际命令硬编码在
各执行器里，杜绝模型幻觉执行任意 shell；模型判断需要看屏幕时输出
escalate，由上层升级到云端 GUI 规划。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from ..config import CLOUD_ENDPOINT, LOCAL_ENDPOINT
from ..device import adb, app_data, ui
from ..llm.client import LLMClient, extract_json, text_msg
from ..llm.prompts import CHAT_SYSTEM, SYSTEM_ROUTER_SYSTEM, SYSTEM_ROUTER_USER


@dataclass
class Capability:
    name: str
    desc: str                        # 给模型看的自然语言说明
    params: str                      # 参数说明（给模型）
    run: Callable[[dict], str]       # 执行器，接收 params dict，返回人话结果


# ── 执行器 ───────────────────────────────────────────
def _query_battery(_p: dict) -> str:
    m = re.search(r"level:\s*(\d+)", adb.run_text("shell", "dumpsys", "battery"))
    return f"当前电量 {m.group(1)}%" if m else "无法获取电量"


def _query_time(_p: dict) -> str:
    return f"当前时间 {adb.run_text('shell', 'date').strip()}"


def _query_storage(_p: dict) -> str:
    out = adb.run_text("shell", "df", "-k", "/data")
    lines = [l for l in out.strip().splitlines() if l.strip()]
    fields = lines[-1].split() if lines else []
    if len(fields) < 4:
        return "无法获取存储"
    return f"/data 可用空间 {int(fields[3]) / 1024 / 1024:.1f} GB"


def _query_memory(_p: dict) -> str:
    m = re.search(r"MemAvailable:\s*(\d+)", adb.run_text("shell", "cat", "/proc/meminfo"))
    return f"可用内存 {int(m.group(1)) / 1024:.0f} MB" if m else "无法获取内存"


def _set_wifi(p: dict) -> str:
    on = bool(p.get("on", False))
    adb.run_text("shell", "svc", "wifi", "enable" if on else "disable")
    return f"WiFi 已{'开启' if on else '关闭'}"


def _set_wifi_auto(p: dict) -> str:
    on = bool(p.get("on", False))
    adb.run_text("shell", "settings", "put", "global", "wifi_wakeup_enabled", "1" if on else "0")
    return f"WiFi自动开启已{'开启' if on else '关闭'}"


def _set_bluetooth(p: dict) -> str:
    on = bool(p.get("on", False))
    adb.run_text("shell", "svc", "bluetooth", "enable" if on else "disable")
    return f"蓝牙已{'开启' if on else '关闭'}"


def _set_brightness(p: dict) -> str:
    lvl = max(0, min(255, int(p.get("level", 128))))
    adb.run_text("shell", "settings", "put", "system", "screen_brightness", str(lvl))
    return f"亮度已设为 {lvl}"


def _set_volume(p: dict) -> str:
    lvl = max(0, min(15, int(p.get("level", 5))))
    adb.run_text("shell", "media", "volume", "--set", str(lvl))
    return f"媒体音量已设为 {lvl}"


def _set_ring_volume(p: dict) -> str:
    # 本机铃声/通知流上限是 7，且 media volume 只改 volume_ring（非 speaker 键）；
    # 判分/系统读的是 volume_ring_speaker，直接写 settings 键才生效。
    lvl = max(0, min(7, int(p.get("level", 3))))
    adb.run_text("shell", "settings", "put", "system", "volume_ring_speaker", str(lvl))
    return f"铃声和通知音量已设为 {lvl}"


def _set_alarm_volume(p: dict) -> str:
    # 闹钟流上限 7；media volume 改的是 volume_alarm，判分读 volume_alarm_speaker。
    lvl = max(0, min(7, int(p.get("level", 3))))
    adb.run_text("shell", "settings", "put", "system", "volume_alarm_speaker", str(lvl))
    return f"闹钟音量已设为 {lvl}"


def _set_airplane(p: dict) -> str:
    on = bool(p.get("on", False))
    adb.run_text("shell", "settings", "put", "global", "airplane_mode_on",
                 "1" if on else "0")
    adb.run_text("shell", "am", "broadcast", "-a",
                 "android.intent.action.AIRPLANE_MODE", "--ez", "state",
                 "true" if on else "false")
    return f"飞行模式已{'开启' if on else '关闭'}"


def _lock_screen(_p: dict) -> str:
    adb.run_text("shell", "input", "keyevent", "26")  # POWER
    return "已锁屏"


def _wake_screen(_p: dict) -> str:
    adb.run_text("shell", "input", "keyevent", "224")  # WAKEUP
    return "已亮屏"


def _set_dark_theme(p: dict) -> str:
    on = bool(p.get("on", True))
    adb.run_text("shell", "settings", "put", "secure", "ui_night_mode", "2" if on else "1")
    return f"已{'开启深色主题' if on else '关闭深色主题'}"


def _set_ring_vibrate(p: dict) -> str:
    on = bool(p.get("on", False))
    adb.run_text("shell", "settings", "put", "system", "vibrate_when_ringing", "1" if on else "0")
    return f"铃声振动已{'开启' if on else '关闭'}"


def _set_private_dns(p: dict) -> str:
    host = (p.get("host") or "").strip()
    if host:
        adb.run_text("shell", "settings", "put", "global", "private_dns_mode", "hostname")
        adb.run_text("shell", "settings", "put", "global", "private_dns_specifier", host)
        return f"私有DNS已设为 {host}"
    adb.run_text("shell", "settings", "put", "global", "private_dns_mode", "off")
    return "私有DNS已关闭"


def _set_app_notification(p: dict) -> str:
    pkg = (p.get("pkg") or "").strip()
    on = bool(p.get("on", True))
    if not pkg:
        return "未指定应用"
    adb.run_text("shell", "cmd", "appops", "set", pkg, "POST_NOTIFICATION",
                 "allow" if on else "ignore")
    return f"{pkg} 通知已{'开启' if on else '关闭'}"


def _set_battery_percent(p: dict) -> str:
    on = bool(p.get("on", True))
    adb.run_text("shell", "settings", "put", "system",
                 "status_bar_show_battery_percent", "1" if on else "0")
    return f"已{'在状态栏显示' if on else '隐藏'}电量百分比"


# (能力名, 说明, 参数说明, 执行器) —— 只增不改命令，模型只能从这里挑
CAPABILITIES: list[Capability] = [
    Capability("query_battery", "查询当前电量", "无参数", _query_battery),
    Capability("query_time", "查询当前时间", "无参数", _query_time),
    Capability("query_storage", "查询剩余存储空间", "无参数", _query_storage),
    Capability("query_memory", "查询可用内存", "无参数", _query_memory),
    Capability("set_wifi", "开启或关闭 WiFi", "on: true/false", _set_wifi),
    Capability("set_wifi_auto", "开启或关闭WiFi自动开启/自动连接", "on: true/false", _set_wifi_auto),
    Capability("set_bluetooth", "开启或关闭蓝牙", "on: true/false", _set_bluetooth),
    Capability("set_brightness", "调整屏幕亮度的明暗程度", "level: 0~255 整数", _set_brightness),
    Capability("set_volume", "调整媒体音量", "level: 0~15 整数", _set_volume),
    Capability("set_ring_volume", "调整铃声和通知音量", "level: 0~15 整数", _set_ring_volume),
    Capability("set_alarm_volume", "调整闹钟音量", "level: 0~15 整数", _set_alarm_volume),
    Capability("set_dark_theme", "开启或关闭深色主题（夜间模式）", "on: true/false", _set_dark_theme),
    Capability("set_ring_vibrate", "开启或关闭来电铃声振动", "on: true/false", _set_ring_vibrate),
    Capability("set_private_dns", "设置私有DNS主机名，留空则关闭", "host: 主机名如 dns.google", _set_private_dns),
    Capability("set_app_notification", "开启或关闭某个应用的通知", "pkg: 包名, on: true/false", _set_app_notification),
    Capability("set_battery_percent", "在状态栏显示或隐藏电量百分比", "on: true/false", _set_battery_percent),
    Capability("set_airplane", "开启或关闭飞行模式", "on: true/false", _set_airplane),
    Capability("lock_screen", "锁屏", "无参数", _lock_screen),
    Capability("wake_screen", "唤醒/点亮屏幕（黑屏变亮）", "无参数", _wake_screen),
]

_BY_NAME = {c.name: c for c in CAPABILITIES}

# 明显的 GUI 线索：出现这些词基本确定要操作屏幕，直接升级云端，省一次模型调用。
# 故意不收「屏幕」「打开」「启动」「进入」等歧义词——「调亮屏幕」是亮度、「打开 WiFi」是开关，
# 这些交给模型判断。
_GUI_CUES = (
    "点击", "滑动", "上滑", "下滑", "左滑", "右滑", "长按", "输入", "打字", "填写",
    "图标", "菜单", "列表", "按钮", "界面", "状态栏", "通知栏", "页面", "设置",
    "浏览器", "微信", "淘宝", "支付宝", "抖音", "京东", "微博", "相机", "相册", "日历",
    "地图", "音乐", "视频", "应用商店", "拨号", "短信", "通讯录",
)


def looks_like_gui(task: str) -> bool:
    return any(c in task for c in _GUI_CUES)


# 明显的「问答/闲聊」线索：这些不是 GUI 动作，本地又没有对应能力，直接明确失败，
# 别让云端为了一个笑话/天气在屏幕上瞎点 12 步（约 22s）。
_CHAT_CUES = ("天气", "笑话", "故事", "新闻", "聊天", "你是谁", "自我介绍",
              "讲个", "说个", "怎么样", "为什么", "什么意思")


def looks_like_chat(task: str) -> bool:
    t = task.strip()
    return t.endswith(("？", "?")) or any(c in t for c in _CHAT_CUES)


def chat_answer(task: str, client: LLMClient) -> Optional[str]:
    """闲聊/问答兜底：用云端模型直接生成文字回答，返回答案文本；失败返回 None。"""
    msg = client.chat(
        CLOUD_ENDPOINT,
        [text_msg("system", CHAT_SYSTEM), text_msg("user", task)],
        temperature=0.7, max_tokens=256,
    )
    return (msg.content or "").strip() or None


def dispatch(name: Optional[str], params: Optional[dict]) -> Optional[str]:
    """执行能力，返回人话结果；未知能力/非法参数返回 None（上层升级云端）。"""
    cap = _BY_NAME.get(name or "")
    if cap is None:
        return None
    try:
        return cap.run(params or {})
    except (ValueError, TypeError):
        return None


# ── 关键词快速路由（确定性，<0.2s）────────────────────
# 0.6B 对这些变化说法不稳定（「关闭蓝牙」「调亮屏幕」常错输出 escalate），
# 关键词表兜底既快又准；表中没有的才交给下面的模型语义路由。

# (触发词, 能力名, 参数)。触发词本身即「修改意图」，无需再判 _ACTION_VERBS。
_KEYWORD_OPS: list[tuple[tuple[str, ...], str, dict]] = [
    (("关闭wifi自动开启", "关wifi自动开启", "wifi自动开启关", "关闭wifi自动连接", "关wifi自动连接"), "set_wifi_auto", {"on": False}),
    (("打开wifi自动开启", "开wifi自动开启", "wifi自动开启开", "打开wifi自动连接"), "set_wifi_auto", {"on": True}),
    (("关闭wifi", "关wifi", "wifi关", "wifi关闭"), "set_wifi", {"on": False}),
    (("打开wifi", "开wifi", "wifi开", "wifi开启"), "set_wifi", {"on": True}),
    (("关闭蓝牙", "关蓝牙", "蓝牙关", "蓝牙关闭"), "set_bluetooth", {"on": False}),
    (("打开蓝牙", "开蓝牙", "蓝牙开", "蓝牙开启"), "set_bluetooth", {"on": True}),
    (("关闭飞行模式", "关飞行模式", "飞行模式关"), "set_airplane", {"on": False}),
    (("打开飞行模式", "开飞行模式", "飞行模式开"), "set_airplane", {"on": True}),
    (("静音", "关声音", "关闭声音", "消音"), "set_volume", {"level": 0}),
    (("显示电量百分比", "电量百分比显示", "显示电池百分比", "状态栏显示电量"), "set_battery_percent", {"on": True}),
    (("隐藏电量百分比", "关闭电量百分比", "取消显示电量", "不显示电量百分比"), "set_battery_percent", {"on": False}),
    (("亮度最高", "最亮", "调到最亮", "亮度最大"), "set_brightness", {"level": 255}),
    (("亮度最低", "最暗", "调到最暗", "亮度最小"), "set_brightness", {"level": 10}),
    (("调亮", "变亮", "提高亮度", "增加亮度", "亮度调高"), "set_brightness", {"level": 220}),
    (("调暗", "变暗", "降低亮度", "减少亮度", "亮度调低"), "set_brightness", {"level": 60}),
    (("铃声音量调大", "铃声调大", "通知音量调大", "铃声音量加大"), "set_ring_volume", {"level": 10}),
    (("铃声音量调小", "铃声调小", "通知音量调小", "铃声音量减小"), "set_ring_volume", {"level": 3}),
    (("闹钟音量调到最大", "闹钟音量最大", "闹钟调大", "闹钟音量调大"), "set_alarm_volume", {"level": 15}),
    (("闹钟音量调到最小", "闹钟音量最小", "闹钟调小", "闹钟音量调小"), "set_alarm_volume", {"level": 0}),
    (("音量调大", "调大音量", "音量加大", "声音调大", "加大音量"), "set_volume", {"level": 10}),
    (("音量调小", "调小音量", "音量减小", "声音调小", "减小音量"), "set_volume", {"level": 3}),
    (("浅色主题", "浅色模式", "亮色模式", "亮色主题", "日间模式", "关闭深色", "关闭暗色", "关闭深色主题"), "set_dark_theme", {"on": False}),
    (("深色主题", "深色模式", "暗色模式", "黑暗模式", "夜间模式", "打开深色", "开启深色", "打开深色主题"), "set_dark_theme", {"on": True}),
    (("关闭铃声振动", "关铃声振动", "铃声振动关", "关闭铃声震动", "关铃声震动"), "set_ring_vibrate", {"on": False}),
    (("打开铃声振动", "开铃声振动", "铃声振动开", "开启铃声振动"), "set_ring_vibrate", {"on": True}),
    (("关闭私有dns", "关私有dns", "私有dns关"), "set_private_dns", {}),
]

# 中文应用名 → 包名。「打开X」这类任务 monkey 拉起 <1s，不必烧云端规划。
# 名字去后缀后精确匹配；表外名字不猜，交给云端（避免拉错应用）。
APP_PACKAGES = {
    "设置": "com.android.settings",
    "相机": "com.android.camera2",
    "浏览器": "com.zui.browser",
    "微信": "com.tencent.mm",
    "音乐": "com.android.music",
    "日历": "com.android.calendar",
    "时钟": "com.android.deskclock",
    "闹钟": "com.android.deskclock",
    "相册": "com.android.gallery3d",
    "图库": "com.android.gallery3d",
    "联系人": "com.android.contacts",
    "通讯录": "com.android.contacts",
    "电话": "com.android.dialer",
    "拨号": "com.android.dialer",
    "计算器": "com.android.calculator2",
    "文件管理": "com.zui.filemanager",
    "文件管理器": "com.zui.filemanager",
    "爱奇艺": "com.qiyi.video.pad",
    "咪咕视频": "com.cmcc.cmvideo",
    "wps": "cn.wps.moffice_eng",
}

_OPEN_RE = re.compile(r"^(打开|启动|进入|开启)\s*(.+)$")


_LEADING_OPEN_RE = re.compile(r"^(打开|启动|进入|开启)\s*(.+?)\s*[,，。；;、]\s*(.+)$")


def launch_leading_app(task: str) -> Optional[str]:
    """任务以「打开X，做Y」开头且 X 是已知应用 → 本地拉起 X，返回剩余子任务 Y；否则 None。

    「打开设置，进入WiFi」这类复合任务，云端常纠结于猜包名（launch）还是回桌面（home），
    都不稳。这里把「打开X」这一步拆出来本地确定地做，剩余「进入WiFi」交给云端看图点，
    可靠性高得多。纯「打开X」由 try_local_intent 处理，不会走到这里。
    """
    m = _LEADING_OPEN_RE.match(task.strip())
    if not m:
        return None
    name = m.group(2).strip()
    pkg = APP_PACKAGES.get(name)
    if not pkg:
        return None
    adb.launch(pkg)
    return m.group(3).strip()


def try_launch_app(task: str) -> Optional[str]:
    """「打开X」→ monkey 拉起 X 的 launcher 入口；成功返回人话，非打开意图/表外返回 None。"""
    m = _OPEN_RE.match(task.strip())
    if not m:
        return None
    name = m.group(2).strip()
    for suffix in ("应用", "app", "App"):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)].strip()
    pkg = APP_PACKAGES.get(name)
    if not pkg:
        return None
    adb.launch(pkg)
    return f"已打开{name}"


def try_local_intent(task: str) -> Optional[str]:
    """关键词系统操作 + 确定性导航 + 打开App 的本地短路（确定性，<0.2s）。

    顺序：系统开关（蓝牙/飞行模式/亮度/音量/WiFi）→ 导航（回桌面/返回）→ 「打开X」。
    表中没有的返回 None，交给模型语义路由 / 云端。
    """
    if not task:
        return None
    # 应用级数据操作（闹钟/日历/联系人）：经 ContentProvider/sqlite3 直接改真机状态。
    ans = app_data.try_alarm_op(task)
    if ans is not None:
        return ans
    ans = app_data.try_calendar_op(task)
    if ans is not None:
        return ans
    ans = app_data.try_contact_op(task)
    if ans is not None:
        return ans
    ans = app_data.try_browser_op(task)
    if ans is not None:
        return ans
    ans = app_data.try_migu_op(task)
    if ans is not None:
        return ans
    # 设置子页面 intent 优先于开关表：「打开蓝牙设置」是进蓝牙设置页，不是打开蓝牙开关
    page = _try_settings_page(task)
    if page is not None:
        return page
    # 「铃声/通知/闹钟/媒体 音量调到 N%」→ 对应音量流（默认媒体）
    mv = re.search(r"(铃声|通知|闹钟|媒体|音乐).{0,4}音量.{0,4}(?:调到|调成|调为|调至)\s*(\d{1,3})\s*%", task)
    if mv:
        stream = mv.group(1)
        lvl = round(int(mv.group(2)) / 100 * 15)
        if "闹钟" in stream:
            return dispatch("set_alarm_volume", {"level": lvl})
        if "媒体" in stream or "音乐" in stream:
            return dispatch("set_volume", {"level": lvl})
        return dispatch("set_ring_volume", {"level": lvl})
    # 「亮度/音量 调到 N%」→ 亮度 / 媒体音量。关键词表只收语义词（调亮/最暗），
    # 具体百分比走这条正则确定性直达，省得漏到云端被误判成「查询亮度」。
    m = re.search(r"(亮度|音量).{0,4}(?:调到|调成|调为|调至)\s*(\d{1,3})\s*%", task)
    if m:
        kind, pct = m.group(1), int(m.group(2))
        if kind == "亮度":
            return dispatch("set_brightness", {"level": round(pct / 100 * 255)})
        return dispatch("set_volume", {"level": round(pct / 100 * 15)})
    # 「私有DNS设为X」→ settings put global（云端点「私有DNS」列表项不可靠）
    md = re.search(r"dns.{0,8}(?:设为|设成|改成|改为|换成|设置为)\s*([a-z0-9][a-z0-9.\-]*\.[a-z0-9.\-]+)", task, re.I)
    if md:
        return dispatch("set_private_dns", {"host": md.group(1).lower()})
    # 「关闭/打开 某应用 的通知」→ cmd appops（比云端点应用通知页稳）
    mn = re.search(r"(关闭|打开|开启|关|开)\s*(.{1,8}?)\s*的?\s*通知", task)
    if mn:
        pkg = APP_PACKAGES.get(mn.group(2).strip())
        if pkg:
            return dispatch("set_app_notification", {"pkg": pkg, "on": mn.group(1) in ("打开", "开启", "开")})
    low = task.lower()
    # 「关闭/打开 WiFi 自动开启/连接」是「自动开启」子开关，不是 WiFi 本体开关，
    # 别被「关闭wifi」关键词误吞成关 WiFi，交给云端（本地无「自动开启」能力）。
    auto_wifi = "wifi" in low and re.search(r"wifi.{0,8}(?:自动开启|自动连接|自动打开)", low) is not None
    for keywords, cap, params in _KEYWORD_OPS:
        if cap == "set_wifi" and auto_wifi:
            continue
        if any(k in low for k in keywords):
            return dispatch(cap, params)
    nav = _try_nav(task)
    if nav is not None:
        return nav
    app = try_launch_app(task)
    if app is not None:
        return app
    # 「点击X」「点X」→ UI 树文本匹配直接 tap（确定性，不调模型、不上云）。
    # 放在关键词/导航/打开App之后，作为 GUI 任务的最后本地兜底；找不到就升级云端。
    mt = re.match(r"^(?:点击|点一下|点|按下|tap)\s*(.{1,12}?)\s*(?:按钮|选项|项|键)?\s*$", task.strip())
    if mt:
        target = mt.group(1).strip()
        # 单字目标（如「点评」里的「评」）太容易误中，要求至少 2 字。
        if len(target) >= 2 and target not in ("屏幕", "图标", "按钮", "页面", "链接", "菜单"):
            ans = ui.tap_by_text(target)
            if ans is not None:
                return ans
    return None


_HOME_WORDS = ("回到桌面", "回桌面", "回主屏幕", "返回桌面", "回主页", "返回主屏幕", "到桌面", "回首页")
_BACK_WORDS = ("返回上一页", "返回上一步", "回上一页", "回上一步", "后退")


def _try_nav(task: str) -> Optional[str]:
    """确定性导航：回桌面 / 返回上一页。不依赖屏幕状态，本地 <0.2s 直接做。"""
    if any(w in task for w in _HOME_WORDS):
        adb.home()
        return "已回到桌面"
    if any(w in task for w in _BACK_WORDS):
        adb.back()
        return "已返回上一页"
    return None


# 设置子页面 → Android settings intent action + 中文回显。
# 「进入WiFi」「打开显示设置」这类直接 am start 系统设置页，比云端看图点列表项稳得多——
# 尤其本机设置是平板两栏布局（master-detail），uiautomator 只 dump 左栏，云端点
# 左栏 WLAN 行（坐标正确）也不跳转，纯靠 intent 才能可靠直达。
SETTINGS_PAGES: list[tuple[tuple[str, ...], str, str]] = [
    (("wifi", "wlan", "无线"), "android.settings.WIFI_SETTINGS", "WiFi"),
    (("蓝牙", "bluetooth"), "android.settings.BLUETOOTH_SETTINGS", "蓝牙"),
    (("显示", "屏幕", "亮度"), "android.settings.DISPLAY_SETTINGS", "显示"),
    (("声音", "音量", "铃声"), "android.settings.SOUND_SETTINGS", "声音"),
    (("电池", "电量"), "android.settings.BATTERY_SAVER_SETTINGS", "电池"),
    (("存储", "空间"), "android.settings.INTERNAL_STORAGE_SETTINGS", "存储"),
    (("位置", "定位"), "android.settings.LOCATION_SOURCE_SETTINGS", "位置"),
    (("飞行模式",), "android.settings.AIRPLANE_MODE_SETTINGS", "飞行模式"),
    (("应用",), "android.settings.APPLICATION_SETTINGS", "应用"),
    (("通知",), "android.settings.NOTIFICATION_SETTINGS", "通知"),
    (("关于", "设备信息"), "android.settings.DEVICE_INFO_SETTINGS", "关于本机"),
]


def _try_settings_page(task: str) -> Optional[str]:
    """「进入WiFi」「打开显示设置」「蓝牙设置」→ am start 对应系统设置页。

    只认两种明确形态：①「进入/转到/到 X」；②「X设置/X页面/X页」（含「打开X设置」）。
    「打开WiFi」「关闭蓝牙」这类开关、「调亮屏幕」这类亮度操作不在这里处理，
    避免和 _KEYWORD_OPS 的开关语义打架。
    """
    t = task.strip()
    core = ""
    m = re.match(r"^(?:进入|转到|到)\s*(.+)$", t)          # 进入WiFi
    if m:
        core = m.group(1).strip()
    else:
        m = re.match(r"^(.+?)(?:设置|页面|页)$", t)         # 蓝牙设置 / 显示页面
        if m:
            core = m.group(1).strip()
        else:
            m = re.match(r"^(?:打开|启动|开启)\s*(.+)$", t)  # 打开蓝牙设置
            if m and re.search(r"(?:设置|页面|页)$", m.group(1).strip()):
                core = re.sub(r"(?:设置|页面|页)$", "", m.group(1).strip()).strip()
    # 「打开显示」「打开存储」「打开关于本机」：不带「设置/页」后缀、但也不是开关类
    # （开关类 wifi/蓝牙/飞行模式 由 _KEYWORD_OPS 兜底），按进设置页处理。
    if not core:
        m = re.match(r"^(?:打开|启动|开启)\s*(显示|存储|关于本机)\s*$", t)
        if m:
            core = m.group(1).strip()
    if not core:
        return None
    low = core.lower()
    for keys, action, label in SETTINGS_PAGES:
        if any(k in low for k in keys):
            adb.run_text("shell", "am", "start", "-a", action)
            return f"已进入{label}"
    return None


def route_with_model(task: str, client: LLMClient) -> Optional[str]:
    """本地模型路由：任务能用系统命令完成 → 执行并返回结果；需看屏幕/失败 → None（升级云端）。"""
    if not task:
        return None
    if looks_like_gui(task):
        return None  # 明显要操作屏幕，直接升级云端，别让模型硬猜
    tools = "\n".join(f"- {c.name}: {c.desc}（参数：{c.params}）" for c in CAPABILITIES)
    system = SYSTEM_ROUTER_SYSTEM.format(tools=tools)
    user = SYSTEM_ROUTER_USER.format(task=task)
    msg = client.chat(
        LOCAL_ENDPOINT,
        [text_msg("system", system), text_msg("user", user)],
        temperature=0.0, max_tokens=64, prefix_cache=True,
    )
    data = extract_json(msg.content)
    if not isinstance(data, dict) or data.get("escalate"):
        return None
    return dispatch(data.get("capability"), data.get("params") or {})
