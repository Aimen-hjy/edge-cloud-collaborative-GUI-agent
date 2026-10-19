# 端侧 Qwen3-0.6B 模型调用说明

部署好的模型通过 `agent_server`（C++ / MNN）对外暴露一个 **OpenAI 兼容的 HTTP 服务**。
其他项目无需关心 MNN、无需重新加载模型，只要像调 OpenAI API 一样发 HTTP 请求即可。

默认地址：`http://127.0.0.1:9090`

---

## 1. 端点速查

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 服务信息 |
| GET | `/health` | 健康检查（探活） |
| POST | `/reset` | 清空 KV Cache（切话题前调用） |
| POST | `/v1/chat/completions` | **核心对话接口（OpenAI 格式）** |
| OPTIONS | `/v1/chat/completions` | CORS 预检 |

---

## 2. 请求格式（`POST /v1/chat/completions`）

```jsonc
{
  "model": "qwen3-0.6b",                              // 任意字符串，仅回显
  "messages": [                                        // 必填，数组
    {"role": "system", "content": "你是助手"},
    {"role": "user",   "content": "现在几点了？"}
  ],
  "temperature": 0.7,                                  // 可选；<=0 走 greedy（确定性）
  "max_tokens": 128,                                   // 可选，默认 2048
  "stream": false,                                     // 可选；true 时返回 SSE 流
  "tools": [                                           // 可选，函数调用工具定义
    {
      "type": "function",
      "function": {
        "name": "get_current_time",
        "description": "获取当前时间",
        "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}}
      }
    }
  ]
}
```

- `messages` 里 `role` 支持 `system` / `user` / `assistant` / `tool`。
- `temperature <= 0` 会被映射为 greedy 采样（MNN 内部 `1/temperature`，传 0 会除零乱码，服务端已兜底）。

---

## 3. 响应格式

非流式（`stream: false`）返回标准 OpenAI 结构：

```jsonc
{
  "id": "chatcmpl-local",
  "object": "chat.completion",
  "model": "qwen3-0.6b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "……",
        "tool_calls": [                              // 有工具调用时才出现
          {
            "id": "call_0",
            "type": "function",
            "function": {"name": "get_current_time", "arguments": "{\"timezone\":\"Asia/Shanghai\"}"}
          }
        ]
      },
      "finish_reason": "stop"                        // "stop" 正常结束；"length" 表示达到 max_tokens
    }
  ],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

流式（`stream: true`）返回 `text/event-stream`（SSE）。

---

## 4. 调用示例

### 4.1 curl（快速验证）

```bash
# 探活
curl -s http://127.0.0.1:9090/health

# 单轮对话
curl -s http://127.0.0.1:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"1+1=?"}],"max_tokens":128}'
```

### 4.2 Python · httpx（零额外依赖）

```python
import httpx

r = httpx.post("http://127.0.0.1:9090/v1/chat/completions", json={
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 128,
    "temperature": 0.7,
}, timeout=60)
print(r.json()["choices"][0]["message"]["content"])
```

### 4.3 Python · openai SDK（几乎零改动接入现有代码）

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:9090/v1", api_key="local")
resp = client.chat.completions.create(
    model="qwen3-0.6b",
    messages=[{"role": "user", "content": "你好"}],
    max_tokens=128,
)
print(resp.choices[0].message.content)
```

### 4.4 Node.js

```js
const r = await fetch("http://127.0.0.1:9090/v1/chat/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ messages: [{ role: "user", content: "你好" }], max_tokens: 128 }),
});
console.log((await r.json()).choices[0].message.content);
```

### 4.5 带工具调用（函数调用）

```python
import httpx

r = httpx.post("http://127.0.0.1:9090/v1/chat/completions", json={
    "messages": [{"role": "user", "content": "现在几点了？"}],
    "tools": [{
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前时间",
            "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}},
        },
    }],
    "temperature": 0,   # 工具调用建议 greedy，结果确定性最高
}, timeout=60)

msg = r.json()["choices"][0]["message"]
print(msg.get("tool_calls"))
# [{"id":"call_0","type":"function","function":{"name":"get_current_time","arguments":"{...}"}}]
```

---

## 5. 地址怎么定（三种访问场景）

| 场景 | base URL |
|---|---|
| 同一台设备 / Termux 内 | `http://127.0.0.1:9090` |
| PC 调设备上的模型 | 先 `adb forward tcp:9090 tcp:9090`，再 `http://127.0.0.1:9090` |
| PC 用自包含包本地跑 | PC 上起 `agent_server`，`http://127.0.0.1:9090` |

---

## 6. 注意事项

1. **`temperature <= 0` = greedy（确定性采样）**。想要可复现结果就传 `0`。
2. **换话题前 `POST /reset`**：清 KV Cache，避免上一段对话污染当前上下文。
3. **服务要活着**：`agent_server` 需在后台运行。设备上 `run_agent.sh` 会自动拉起；PC 上手动起。调用前可先 `GET /health` 探活。
4. **多轮对话**：把历史消息全部放进 `messages` 数组即可（服务端有 KV Cache 复用，增量 prefill 会加速）。
