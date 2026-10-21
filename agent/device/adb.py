"""adb 薄封装：截图 / 抓 UI 树 / 注入动作。

全部走 `adb` 子进程，不引入 uiautomator2 等第三方依赖，行为透明、可控。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from ..config import ADB_SERIAL


def _prefix() -> list[str]:
    """adb 命令前缀，带可选序列号（多设备时用 ADB_SERIAL 指定）。"""
    cmd = ["adb"]
    if ADB_SERIAL:
        cmd += ["-s", ADB_SERIAL]
    return cmd


def run_text(*args: str, timeout: int = 30) -> str:
    """执行 adb 命令，返回 stdout 文本（失败返回空串）。

    adb 输出是 UTF-8（UI XML 里含中文），而 Windows 上 subprocess 默认按
    GBK 解码，必须显式指定 UTF-8，否则含中文的输出会抛 UnicodeDecodeError。
    """
    try:
        p = subprocess.run(
            _prefix() + list(args), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.stdout or ""
    except Exception:
        return ""


def run_bytes(*args: str, timeout: int = 30) -> bytes:
    """执行 adb 命令，返回 stdout 原始字节（用于截图）。"""
    try:
        p = subprocess.run(
            _prefix() + list(args), capture_output=True, timeout=timeout
        )
        return p.stdout or b""
    except Exception:
        return b""


# ── 观察 ─────────────────────────────────────────────
def screencap() -> bytes:
    """当前屏幕截图，PNG 字节（全分辨率，含设备端 ~0.5s PNG 编码）。"""
    return run_bytes("exec-out", "screencap", "-p")


# 设备端低分辨率抓屏工具（server/screencap_fast.cpp 的编译产物）。
# 只读帧缓冲 → 内存里 area-average 下采样到长边 max_side → 编码小 PNG，
# 省掉设备端全分辨率 libpng 编码的 ~0.5s。
_SCREENCAP_FAST_LOCAL = Path(__file__).resolve().parent.parent.parent / "server" / "screencap_fast"
_SCREENCAP_FAST_REMOTE = "/data/local/tmp/screencap_fast"
_screencap_fast_ready: Optional[bool] = None  # None=未探测, True=可用, False=不可用
_screen_dims: Optional[tuple[int, int]] = None  # 屏幕分辨率缓存（旋转罕见，缓存省每次 wm size 往返）


def _push_screencap_fast() -> bool:
    """把 screencap_fast 推送到设备并 chmod，返回是否就位。"""
    if not _SCREENCAP_FAST_LOCAL.exists():
        return False
    run_text("push", str(_SCREENCAP_FAST_LOCAL), _SCREENCAP_FAST_REMOTE)
    run_text("shell", "chmod", "755", _SCREENCAP_FAST_REMOTE)
    return "screencap_fast" in run_text("shell", "ls", "-l", _SCREENCAP_FAST_REMOTE)


def screencap_fast(max_side: int = 1024) -> bytes:
    """低分辨率抓屏（长边 max_side），失败自动回退 screencap()。

    首次调用时探测并推送原生工具；工具不可用或输出非法图片时回退到全分辨率
    screencap()，保证调用方永远拿到一张可用截图。输出是 JPEG（比 PNG 小 ~9x）。
    """
    global _screencap_fast_ready, _screen_dims
    if _screencap_fast_ready is None:
        _screencap_fast_ready = _push_screencap_fast()
    if not _screencap_fast_ready:
        return screencap()
    if _screen_dims is None:
        _screen_dims = screen_size()
    w, h = _screen_dims
    out = run_bytes(
        "exec-out", "sh", "-c",
        f"screencap | {_SCREENCAP_FAST_REMOTE} {w} {h} {max_side}",
    )
    if out.startswith(b"\x89PNG") or out.startswith(b"\xff\xd8\xff"):
        return out
    return screencap()


def dump_ui() -> str:
    """抓取当前屏幕 UI 层级 XML。

    优先用 `uiautomator dump /dev/tty` 直接拿到 stdout；失败时退化为
    先 dump 到 /sdcard 再 cat 回来。返回从 `<?xml` 开始的纯净 XML 文本。
    """
    out = run_text("exec-out", "uiautomator", "dump", "/dev/tty")
    xml = _extract_xml(out)
    if xml:
        return xml

    run_text("shell", "uiautomator", "dump", "/sdcard/ui_dump.xml")
    out = run_text("shell", "cat", "/sdcard/ui_dump.xml")
    return _extract_xml(out)


def _extract_xml(out: str) -> str:
    idx = out.find("<?xml")
    if idx == -1:
        return ""
    end = out.find("</hierarchy>")
    if end == -1:
        return out[idx:]
    return out[idx:end + len("</hierarchy>")]


# ── 执行 ─────────────────────────────────────────────
def tap(x: int, y: int) -> None:
    run_text("shell", "input", "tap", str(x), str(y))


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    run_text("shell", "input", "swipe", str(x1), str(y1),
             str(x2), str(y2), str(duration_ms))


def input_text(text: str) -> None:
    """输入文本。仅支持 ASCII；中文需 ADBKeyboard（Phase 2 补充）。"""
    text = text.replace(" ", "%s")
    run_text("shell", "input", "text", text)


def keyevent(code: str) -> None:
    run_text("shell", "input", "keyevent", code)


def back() -> None:
    keyevent("KEYCODE_BACK")


def home() -> None:
    keyevent("KEYCODE_HOME")


def launch(package: str) -> None:
    """拉起 App 的 launcher 入口。

    先 `cmd package resolve-activity --brief` 解析主 Activity，再 `am start -n`
    直接拉起，比 monkey（~1.5s）快约 3 倍（~0.4s），且不依赖硬编码 Activity 名。

    注意：不能写成 `sh -c 'act=$(...) ; am start -n "$act"'`——adb 把整段交给
    设备外层 shell 时，`$(...)`/`$act` 会被外层先展开成空，导致 Bad component name。
    这里拆成两次独立 adb 调用，不经过 shell 变量，最稳。
    """
    out = run_text("shell", "cmd", "package", "resolve-activity", "--brief", package)
    act = next((ln.strip() for ln in out.splitlines() if "/" in ln), "")
    if act:
        run_text("shell", "am", "start", "-n", act)


def screen_size() -> tuple[int, int]:
    """屏幕物理分辨率 (宽, 高)。失败时给一个常见的兜底值。"""
    out = run_text("shell", "wm", "size")
    m = re.search(r"(\d+)\s*x\s*(\d+)", out)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1080, 2340
