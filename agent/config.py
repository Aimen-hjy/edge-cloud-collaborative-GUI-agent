"""全局配置：端侧/云端 LLM 端点、运行参数。

端侧 = 本地 agent_server（OpenAI 兼容，见 server/API.md），默认 127.0.0.1:9090。
云端 = 阿里百炼兼容模式，默认 qwen3-vl-max（多模态，看图决策）。

所有关键项都可用环境变量覆盖，方便在手机 / PC 之间切换。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class LLMEndpoint:
    """一个 OpenAI 兼容的 LLM 端点。"""
    base_url: str
    model: str
    api_key: str = "local"  # 端侧无需真实 key，填个占位即可


# ── 端侧：本地 agent_server ──────────────────────────
# 注意：model 字段仅回显，实际模型由 agent_server 启动时 -c 的 config.json 决定。
# 端侧现在跑 Qwen3-VL-2B（视觉），既能文本 grounding 也能视觉 grounding。
LOCAL_ENDPOINT = LLMEndpoint(
    base_url=os.getenv("LOCAL_BASE_URL", "http://127.0.0.1:9090/v1"),
    model=os.getenv("LOCAL_MODEL", "qwen3-vl-2b"),
    api_key="local",
)

# ── 云端：阿里百炼 qwen3-vl-max（多模态旗舰）─────────
CLOUD_ENDPOINT = LLMEndpoint(
    base_url=os.getenv(
        "CLOUD_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    model=os.getenv("CLOUD_MODEL", "qwen3-vl-max"),
    api_key=os.getenv("DASHSCOPE_API_KEY", ""),
)

# ── 运行参数 ─────────────────────────────────────────
MAX_STEPS: int = int(os.getenv("MAX_STEPS", "12"))            # 单任务最大步数
STUCK_THRESHOLD: int = int(os.getenv("STUCK_THRESHOLD", "3"))  # 连续无进展步数阈值
ACTION_SETTLE_SEC: float = float(os.getenv("ACTION_SETTLE_SEC", "0.5"))  # 动作后等待 UI 稳定

# adb 设备序列号；留空 = 默认唯一设备
ADB_SERIAL: str = os.getenv("ADB_SERIAL", "")
