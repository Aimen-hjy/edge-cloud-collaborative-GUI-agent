# 端云协同 GUI Agent —— 架构说明

一个 Android 上的自然语言 GUI 自动化 Agent：**云端多模态大模型当"大脑"（看图规划），
端侧本地模型当"手眼"（系统操作路由 + 接地回退），adb 当"手脚"（执行）。**

核心目标：**90% 任务成功率 + <1s 单步延迟**。所有关键设计都围绕"在延迟和成本可控的前提下，
把每一步该干的活放到最便宜、最快的层级去"展开。

---

## 1. 总体架构

```
                         ┌─────────────────────────────────────────────┐
  自然语言任务 ─────────▶│  层1  本地系统查询表（dumpsys）   <0.2s 命中直接返回  │
                         │  层2  本地模型系统路由（Qwen3-VL-2B） ~1.7s 失败升级  │
                         └──────────────────┬──────────────────────────┘
                                             │ 需要看图（点控件/开App/输入）
                                             ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  层3/4  云端 GUI 主循环（agent/loop.py）                          │
        │                                                                │
        │   screencap ─▶ 降采样(1024) ─▶ 云端VL看图 ─▶ 动作+归一化坐标      │
        │        ▲                            │                          │
        │        │                            ▼                          │
        │        │                     adb 执行 tap/type/swipe/...        │
        │        │                            │                          │
        │        └──── 截图指纹校验（8×8灰度）◀┘ 屏幕没变→跳过下次云端        │
        └────────────────────────────────────────────────────────────────┘
```

两端都走 **OpenAI 兼容协议**，用同一个 `LLMClient`（[agent/llm/client.py](agent/llm/client.py)），
切换 `base_url` 即可，prompt / JSON 解析逻辑复用。

---

## 2. 分层路由（按需付费：不为不需要智能的步骤付模型/UI 树成本）

| 层级 | 处理者 | 覆盖的任务 | 耗时（热） | 失败行为 |
|---|---|---|---|---|
| **层1 关键词查询** | `agent/device/system_query.py` | 电量 / 时间 / 存储 / 内存 / 亮度 / WiFi / 蓝牙 | **~0.12s** | 不匹配则落到层1.5 |
| **层1.5 本地确定性** | `system_ops.try_local_intent` | 系统开关（关蓝牙/调亮度/静音）、打开 App、回桌面/返回 | **~0.4s** | 表外则落到层1.6 |
| **层1.6 复合拆解** | `system_ops.launch_leading_app` | 「打开X，做Y」→ 本地拉起 X，Y 交云端 | **~0.4s** | 非「打开X」前缀则落层2 |
| **层2 模型系统路由** | 本地 Qwen3-**0.6B**（`agent/tools/system_ops.py`） | 关键词表接不住的变化说法 | **~0.5~0.9s** | 输出 `escalate` 升级云端 |
| **层3/4 云端 GUI 循环** | 云端 `qwen3-omni-flash` | 点控件 / 输入 / 滑动 | **~2.5s/步** | — |

- 层1 / 1.5 / 1.6 都是**确定性代码**（shell 命令 + 关键词表 + 包名表），不经过任何模型，
  是成功率与延迟的压舱石——0.6B 对这些变化说法不稳定（常错输出 escalate）。
- 层2 的模型只负责"选能力 + 填参数"，**实际命令硬编码在 `CAPABILITIES` 表里**，
  杜绝模型幻觉执行任意 shell；判断需要看屏幕时输出 `escalate`。
- 层3/4 才是真正"看图点控件"的活儿，是当前单步耗时 >1s 的主要来源。

---

## 3. 主循环：observe → plan(云) → act → verify

见 [agent/loop.py](agent/loop.py) 的 `run_task()`，单步流程：

1. **抓屏** `screencap`（PNG，~0.7s）。
2. **跳过云端快路径**：把当前截图和上一步的截图做指纹比对，**一模一样就直接跳过云端**
   （说明上一步点空 / 还在加载），等一拍重截图，记一条 `wait` 无进展，避免白烧一次云端调用。
3. **云端规划**：截图先降采样到长边 1024（[screen.resize_png](agent/device/screen.py)），
   连同任务 + 历史发给云端 VL，返回一个 JSON：
   ```json
   {"action": "tap", "x": 570, "y": 926}
   ```
   `x/y` 是 **0~1000 的归一化坐标**，与截图分辨率无关。
4. **执行**：`tap`/`type` 走快路径——把归一化坐标换算成像素直接注入（`_cloud_coord`），
   **省掉 dump_ui + 本地 grounding**；云端没给有效坐标时才回退到本地 grounding。
5. **校验**：等 UI 稳定后抓新截图，用 8×8 灰度指纹判断页面是否变化，
   没有 UI 树也能判断（`img_changed`）。
6. **卡死检测**：连续 `STUCK_THRESHOLD`（默认 3）步页面无进展 → 提前中止，避免空转。

支持的动作：`tap` / `type` / `swipe` / `back` / `home` / `launch` / `query` / `done` / `fail`。

---

## 4. 端云分工

| 职责 | 谁来做 | 说明 |
|---|---|---|
| 看图决策下一步动作 | **云端** `qwen3-omni-flash` | 多模态，看截图 + 任务 + 历史 → 动作 JSON |
| 目标定位（接地） | **云端直接出坐标**，端侧回退 | 主路径云端给归一化坐标；没坐标时回退到本地 grounding |
| 系统操作语义路由 | **端侧** Qwen3-VL-2B | 层2，把自然语言映射到安全的能力表 |
| 文本接地（回退） | **端侧** Qwen3-VL-2B | 目标描述 + UI 树 → 控件坐标 |
| 视觉接地（回退） | **端侧** Qwen3-VL-2B | 截图 + 目标 → 原生 box 格式坐标 |
| 执行注入 | **adb** | tap / swipe / type / keyevent / monkey |

**为什么主路径把接地也交给云端？** 实测发现单步耗时的隐性大头不是云端，而是
`dump_ui`（~1s）+ 本地 grounding（~5~18s）。让云端在规划时顺便把坐标也给了，
tap/type 一步从 15~28s 降到 ~3s，且坐标用归一化值天然分辨率无关。

---

## 5. 关键设计决策

### 5.1 云端直接出归一化坐标（0~1000）
- 省掉 `dump_ui` + 本地 grounding 两个慢环节。
- 归一化坐标与截图分辨率无关，天然配合降采样上传。
- **保留本地 grounding 作为回退**：云端没给有效坐标时（`_cloud_coord` 返回 None），
  仍走 `dump_ui` + 文本接地 / 视觉接地。

### 5.2 截图指纹（8×8 灰度）做变化检测
- `img_signature` 把 PNG 缩成 8×8 灰度元组，粗粒度缩放能容忍状态栏时钟、图标微动等噪声。
- 用于两处：① 无 UI 树时的"页面是否变化"校验；② 跳过云端快路径的"屏幕是否没变"判断。

### 5.3 屏幕不变就跳过云端
- 上一步动作没生效（点空 / 加载中）时，云端看到同一张图多半给出同样动作，重复问纯白烧 ~1s。
- 改为等一拍重截图 + 记 `wait`，靠卡死检测连续无进展提前止损。

### 5.4 降采样上传（长边 1024）
- VL 模型的 image token 数与像素量正相关，全分辨率 1600×2400 截图（~2.2MB）的
  prefill 和上传都被它拖慢。缩到 683×1024 后 image token 掉 3.5×。
- 接地精度几乎无损（归一化坐标免疫分辨率变化）。

### 5.5 精简 prompt 输出
- tap/type 的 JSON 里删掉冗余的 `target` 中文描述（坐标已经给了），收紧 system prompt，
  砍掉 decode 阶段的 token 数。

### 5.6 模型选型（本会话实测）
| 模型 | 云端单步 | 定位能力 |
|---|---|---|
| `qwen3-omni-flash`（**当前**） | 0.6~1.4s | ✅ 图标点中，误差 ~10px |
| `qwen3.8-omni-flash`（上一版） | 3~5s | ✅ 同上 |
| `qwen3-vl-plus` | 1.1~1.8s | ❌ 只会 swipe up，找不到图标 |
| `gui-plus` | 0.8~2s | ⚠️ 另一套协议 + 包围盒松散点不中 |

结论：`qwen3-omni-flash` 是"快且准"的甜点位。更小的模型虽快但定位塌方，会砸掉成功率。

### 5.7 本地路由前缀缓存
- 层2 路由的 system 提示词跨轮恒定，`agent_server` 端 `prefix_cache=true` 把恒定前缀
  的 KV prefill 一次复用多轮，路由请求从冷 ~6.5s 压到热 **~0.5~0.9s**（手写
  `AnswerWithPrefix`，见 `tools/agent_server.cpp`；MNN 自带 prompt_cache 不能缓存共享前缀）。

---

## 6. 延迟模型（热启动实测）

| 路径 | 平均耗时 |
|---|---|
| 层1 关键词查询（纯 shell） | ~0.12s |
| 层1.5 本地确定性（开关/打开App/导航） | **~0.4s**（`am start` 解析启动，比 monkey ~1.5s 快 4x） |
| 层2 本地模型路由（前缀缓存热） | ~0.5~0.9s |
| 云端单步（`qwen3-omni-flash`） | **~2.5s**（云端 0.55~0.9s + 执行 0.2~0.6s + 校验 ~1.2s） |
| `screencap_fast` 单次（JPEG） | **~0.87s**（读帧缓冲 ~0.41s 是地板 + 下采样/JPEG 编码 + adb 传输 ~94KB） |

**单步耗时构成（当前，云端步）**：`screencap_fast 0.87s + 云端推理 0.55~0.9s + 执行 ~0.2s + 校验(0.5s settle + 0.87s 截图)`。

**JPEG 化（本轮）**：截图从 683×1024 PNG（~842KB）换成 JPEG（~94KB），adb 传输和
云端上传都小 ~9x，云端推理从 ~1.2s 降到 ~0.55s。读帧缓冲 ~0.41s 是抓屏地板，是
"单步 <1s" 目标的主要硬约束之一。

---

## 7. 目录结构

| 路径 | 职责 |
|---|---|
| `agent/main.py` | 命令行入口 |
| `agent/config.py` | 端/云端点、API Key、运行参数（环境变量可覆盖） |
| `agent/loop.py` | 主循环：路由 + 规划 + 执行 + 校验 + 卡死检测 |
| `agent/router.py` | 路由策略（卡死检测；Phase 2 扩展点） |
| `agent/llm/client.py` | 统一 OpenAI client + JSON 解析 |
| `agent/llm/prompts.py` | 云端规划 / 接地 / 系统路由的 prompt 模板 |
| `agent/device/adb.py` | adb 封装：截图 / UI 树 / 注入 |
| `agent/device/screen.py` | UI 树 XML → 控件列表；截图指纹；降采样 |
| `agent/device/system_query.py` | 层1：关键词 → dumpsys 查询 |
| `agent/tools/system_ops.py` | 层2：本地模型系统操作路由 + 能力表 |
| `agent/tools/actions.py` | 动作执行器 + 坐标合法性校验 |
| `agent/perception/ground.py` | 接地回退：文本接地 + 视觉接地 |
| `agent/memory/context.py` | 执行历史 + 卡死计数 |
| `server/` | 端侧模型服务（C++ / MNN，OpenAI 兼容，见 [server/API.md](server/API.md)） |

---

## 8. 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOCAL_BASE_URL` | `http://127.0.0.1:9090/v1` | 端侧模型地址 |
| `LOCAL_MODEL` | `qwen3-vl-2b` | 端侧模型名（仅回显） |
| `CLOUD_BASE_URL` | 百炼兼容模式 | 云端地址 |
| `CLOUD_MODEL` | `qwen3-omni-flash` | 云端多模态模型 |
| `CLOUD_IMG_MAX_SIDE` | `1024` | 上传前截图长边像素（降采样） |
| `DASHSCOPE_API_KEY` | 读 `qwen3.8-apikey` | 云端 API Key（环境变量优先，否则读文件） |
| `ADB_SERIAL` | — | 多设备时指定序列号 |
| `MAX_STEPS` | `12` | 单任务最大步数 |
| `STUCK_THRESHOLD` | `3` | 连续无进展步数阈值（卡死判定） |
| `ACTION_SETTLE_SEC` | `0.5` | 动作后等待 UI 稳定的秒数 |

---

## 9. 已知限制与待办

- **中文输入**：`adb input text` 只支持 ASCII，中文需 ADBKeyboard。
- **launch 按包名**（已做）：`system_ops.APP_PACKAGES` 中文名→包名，`adb.launch` 用
  `cmd package resolve-activity` + `am start`（比 monkey 快 4x）。图标不在当前屏也不再靠
  模型猜。
- **done 确认步白烧一次云端调用**：第 2 步纯为让模型说"已完成"，浪费 ~1s。
  待做：动作后本地指纹变化即收尾，或让模型更激进地提前 done。
- **screencap 0.87s**（已做 JPEG 化）：读帧缓冲 ~0.41s 是地板，下采样后编 JPEG
  （~94KB）替代 PNG（~842KB），adb 传输 + 云端上传都小 ~9x。剩余 ~0.45s 是
  设备端管道/编码 + adb 往返开销。
- **云端弱模型导航不稳**（核心遗留）：`qwen3-omni-flash` 在"打开屏幕上没有的 App"、
  "进入X页面"时易输出 home/swipe/query 而非点击可见控件，是成功率的主要天花板。
  缓解：层1.5/1.6 本地确定性兜底 + 复合任务拆解，把云端看图步骤压到最少。
