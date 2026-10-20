# 端云协同 GUI Agent（Android）

云端多模态大模型当"大脑"（规划 + 看图决策），端侧 Qwen3-0.6B 当"手眼"
（grounding 定位坐标），adb 负责执行。两端都走 OpenAI 兼容协议，一套 client 复用。

## 架构

```
云端 qwen3-vl-max ──看截图+任务──▶ 决定动作 + 目标描述
                                    │
端侧 qwen3-0.6b  ──目标描述+UI树──▶ 定位控件坐标（grounding）
                                    │
adb ──────────────────────────────▶ 注入 tap/swipe/type/...
```

主循环：`observe → plan(云) → ground(端) → act → verify`，见 [agent/loop.py](agent/loop.py)。

## 目录

| 路径 | 职责 |
|---|---|
| `agent/config.py` | 端/云端点、运行参数（环境变量可覆盖） |
| `agent/llm/client.py` | 统一 OpenAI client + JSON 解析 |
| `agent/llm/prompts.py` | 云端规划 / 端侧 grounding 模板 |
| `agent/device/adb.py` | adb 封装：截图 / UI 树 / 注入 |
| `agent/device/screen.py` | UI 树 XML → 控件列表 |
| `agent/perception/ground.py` | 目标描述 → 控件坐标 |
| `agent/tools/actions.py` | 动作执行 + 坐标校验 |
| `agent/memory/context.py` | 历史记录 + 卡死检测 |
| `agent/router.py` | 端云路由（Phase 2 扩展点） |
| `agent/loop.py` | 主循环 |
| `server/` | 端侧模型服务（已有，见 server/API.md） |

## 前置条件

1. **端侧模型**：按 [server/API.md](server/API.md) 启动 `agent_server`，默认
   `http://127.0.0.1:9090`。手机跑模型时先 `adb forward tcp:9090 tcp:9090`。
2. **设备**：`adb devices` 能看到目标手机，已开启 USB 调试。
3. **云端 key**：设置环境变量 `DASHSCOPE_API_KEY`（阿里百炼）。

## 安装 & 运行

```bash
pip install -r requirements.txt

# 探活
curl -s http://127.0.0.1:9090/health

# 跑一个任务
python -m agent.main "打开设置"
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOCAL_BASE_URL` | `http://127.0.0.1:9090/v1` | 端侧模型地址 |
| `LOCAL_MODEL` | `qwen3-0.6b` | 端侧模型名（仅回显） |
| `CLOUD_BASE_URL` | 百炼兼容模式 | 云端地址 |
| `CLOUD_MODEL` | `qwen3-vl-max` | 云端多模态模型 |
| `DASHSCOPE_API_KEY` | — | 云端 API Key（必填） |
| `ADB_SERIAL` | — | 多设备时指定序列号 |
| `MAX_STEPS` / `STUCK_THRESHOLD` | `12` / `3` | 步数上限 / 卡死阈值 |

## 已知限制（Phase 2/3 解决）

- **中文输入**：`adb input text` 只支持 ASCII，中文需 ADBKeyboard。
- **每步都调云端**：成本偏高；Phase 2 加端侧快速通道（高置信度简单步本地决策）。
- **端侧仍是文本 0.6B**：切 Qwen3-VL-2B 视觉 grounding 需升级 server 的 MNN 配置。
- **截图/UI 树串行抓取**：约 1~2s/步，后续可并行 + 增量缓存压进 1s。
