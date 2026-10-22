"""全局配置：端侧/云端 LLM 端点、运行参数。

端侧 = 本地 agent_server（OpenAI 兼容，见 server/API.md），默认 127.0.0.1:9090。
云端 = 阿里百炼兼容模式，默认 qwen3-omni-flash（多模态，看图决策）。

所有关键项都可用环境变量覆盖，方便在手机 / PC 之间切换。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMEndpoint:
    """一个 OpenAI 兼容的 LLM 端点。"""
    base_url: str
    model: str
    api_key: str = "local"  # 端侧无需真实 key，填个占位即可


# ── 端侧：本地 agent_server ──────────────────────────
# 注意：model 字段仅回显，实际模型由 agent_server 启动时 -c 的 config.json 决定。
# 端侧现在跑 Qwen3-0.6B（纯文本），只做系统操作路由 + 文本 grounding，无视觉能力。
LOCAL_ENDPOINT = LLMEndpoint(
    base_url=os.getenv("LOCAL_BASE_URL", "http://127.0.0.1:9090/v1"),
    model=os.getenv("LOCAL_MODEL", "qwen3-0.6b"),
    api_key="local",
)

# ── 云端：阿里百炼 qwen3.8-omni-flash（多模态旗舰）───
def _read_api_key() -> str:
    """从项目根目录的 qwen3.8-apikey 读取云端 API Key（该文件已 gitignore）。

    优先级：环境变量 DASHSCOPE_API_KEY > 密钥文件。文件不存在时返回空串。
    """
    env = os.getenv("DASHSCOPE_API_KEY")
    if env:
        return env
    key_file = Path(__file__).resolve().parent.parent / "qwen3.8-apikey"
    try:
        return key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


CLOUD_ENDPOINT = LLMEndpoint(
    base_url=os.getenv(
        "CLOUD_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    model=os.getenv("CLOUD_MODEL", "qwen3-omni-flash"),
    api_key=_read_api_key(),
)

# 上传云端前把截图长边压到这个像素值：VL 模型的 image token 数与像素量正相关，
# 压小图能显著砍 prefill + 上传耗时，且归一化坐标(0~1000)不受分辨率影响。
# 768 是精度/速度折中：比 1024 少 ~44% 像素，prefill 显著下降，点控件误差仍 <15px。
CLOUD_IMG_MAX_SIDE: int = int(os.getenv("CLOUD_IMG_MAX_SIDE", "768"))

# ── 运行参数 ─────────────────────────────────────────
MAX_STEPS: int = int(os.getenv("MAX_STEPS", "12"))            # 单任务最大步数
STUCK_THRESHOLD: int = int(os.getenv("STUCK_THRESHOLD", "3"))  # 连续无进展步数阈值
ACTION_SETTLE_SEC: float = float(os.getenv("ACTION_SETTLE_SEC", "0.2"))  # 动作后等待 UI 稳定（抓屏自身的 ~0.4s 帧缓冲读取就是天然 settle）

# adb 设备序列号；留空 = 默认唯一设备
ADB_SERIAL: str = os.getenv("ADB_SERIAL", "")
