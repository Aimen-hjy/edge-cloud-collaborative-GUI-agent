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
from ..device import adb
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


# (能力名, 说明, 参数说明, 执行器) —— 只增不改命令，模型只能从这里挑
CAPABILITIES: list[Capability] = [
    Capability("query_battery", "查询当前电量", "无参数", _query_battery),
    Capability("query_time", "查询当前时间", "无参数", _query_time),
    Capability("query_storage", "查询剩余存储空间", "无参数", _query_storage),
    Capability("query_memory", "查询可用内存", "无参数", _query_memory),
    Capability("set_wifi", "开启或关闭 WiFi", "on: true/false", _set_wifi),
    Capability("set_bluetooth", "开启或关闭蓝牙", "on: true/false", _set_bluetooth),
    Capability("set_brightness", "调整屏幕亮度的明暗程度", "level: 0~255 整数", _set_brightness),
    Capability("set_volume", "调整媒体音量", "level: 0~15 整数", _set_volume),
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
    (("关闭wifi", "关wifi", "wifi关", "wifi关闭"), "set_wifi", {"on": False}),
    (("打开wifi", "开wifi", "wifi开", "wifi开启"), "set_wifi", {"on": True}),
    (("关闭蓝牙", "关蓝牙", "蓝牙关", "蓝牙关闭"), "set_bluetooth", {"on": False}),
    (("打开蓝牙", "开蓝牙", "蓝牙开", "蓝牙开启"), "set_bluetooth", {"on": True}),
    (("关闭飞行模式", "关飞行模式", "飞行模式关"), "set_airplane", {"on": False}),
    (("打开飞行模式", "开飞行模式", "飞行模式开"), "set_airplane", {"on": True}),
    (("静音", "关声音", "关闭声音", "消音"), "set_volume", {"level": 0}),
    (("亮度最高", "最亮", "调到最亮", "亮度最大"), "set_brightness", {"level": 255}),
    (("亮度最低", "最暗", "调到最暗", "亮度最小"), "set_brightness", {"level": 10}),
    (("调亮", "变亮", "提高亮度", "增加亮度", "亮度调高"), "set_brightness", {"level": 220}),
    (("调暗", "变暗", "降低亮度", "减少亮度", "亮度调低"), "set_brightness", {"level": 60}),
    (("音量调大", "调大音量", "音量加大", "声音调大", "加大音量"), "set_volume", {"level": 10}),
    (("音量调小", "调小音量", "音量减小", "声音调小", "减小音量"), "set_volume", {"level": 3}),
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
    # 设置子页面 intent 优先于开关表：「打开蓝牙设置」是进蓝牙设置页，不是打开蓝牙开关
    page = _try_settings_page(task)
    if page is not None:
        return page
    low = task.lower()
    for keywords, cap, params in _KEYWORD_OPS:
        if any(k in low for k in keywords):
            return dispatch(cap, params)
    nav = _try_nav(task)
    if nav is not None:
        return nav
    return try_launch_app(task)


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
