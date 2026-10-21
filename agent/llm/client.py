"""统一 LLM Client。

端侧 agent_server 和云端百炼都是 OpenAI 兼容协议，这里用一个 OpenAI 客户端、
只切换 base_url 即可。这是整个端云协同的地基：prompt / 输出解析两端共用。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

import httpx
from openai import OpenAI

from ..config import LLMEndpoint


class LLMClient:
    """按 base_url 缓存 OpenAI 客户端，端/云互不干扰。"""

    def __init__(self) -> None:
        self._clients: dict[str, OpenAI] = {}

    def _get(self, endpoint: LLMEndpoint) -> OpenAI:
        if endpoint.base_url not in self._clients:
            self._clients[endpoint.base_url] = OpenAI(
                base_url=endpoint.base_url,
                api_key=endpoint.api_key or "local",
            )
        return self._clients[endpoint.base_url]

    def chat(self, endpoint: LLMEndpoint, messages: list[dict],
             temperature: float = 0.0, max_tokens: int = 1024,
             prefix_cache: bool = False):
        """发起一次对话，返回 choices[0].message（含 .content / .tool_calls）。

        prefix_cache=True 时透传 agent_server 的前缀缓存开关：路由请求的 system
        提示词跨轮恒定，服务端 prefill 一次后每轮只补「任务」delta，省下重复 prefill。
        """
        client = self._get(endpoint)
        resp = client.chat.completions.create(
            model=endpoint.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"prefix_cache": True} if prefix_cache else None,
        )
        return resp.choices[0].message

    def reset_kv(self, endpoint: LLMEndpoint) -> None:
        """清空端侧 KV Cache（换任务前调用，避免上轮对话污染当前上下文）。"""
        root = endpoint.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        try:
            httpx.post(f"{root}/reset", timeout=10)
        except Exception:
            pass  # 重置失败不影响主流程，仅影响多轮缓存复用


# ── 消息构造助手 ─────────────────────────────────────
def text_msg(role: str, content: str) -> dict:
    """纯文本消息。"""
    return {"role": role, "content": content}


def _image_mime(data: bytes) -> str:
    """按魔数识别图片类型，供 data URL 使用。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "image/png"


def image_msg(role: str, text: str, image_bytes: bytes) -> dict:
    """文本 + 一张图（PNG/JPEG 字节）的消息，OpenAI 视觉格式。"""
    return {
        "role": role,
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {
                "url": f"data:{_image_mime(image_bytes)};base64,{image_b64(image_bytes)}",
            }},
        ],
    }


def image_b64(img_bytes: bytes) -> str:
    """图片字节 → base64 字符串。"""
    return base64.b64encode(img_bytes).decode()


# ── 输出解析 ─────────────────────────────────────────
def extract_json(text: Optional[str]) -> Optional[Any]:
    """从模型输出里稳健地抠出第一个平衡的 JSON 对象。

    兼容常见的“带说明 + ```json 代码块”等噪声输出。
    """
    if not text:
        return None
    s = text.strip()
    # 去掉 markdown 代码围栏
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"\s*```$", "", s, flags=re.MULTILINE)

    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
    try:
        return json.loads(s[start:])
    except json.JSONDecodeError:
        return None
