"""端/云共用的 Prompt 模板。

- PLANNER_*：给云端多模态模型，看图 + 任务 + 历史 → 决定下一步动作 + 目标描述。
- GROUNDER_*：给端侧文本模型，目标描述 + 控件列表 → 定位控件编号。
"""

# ── 云端规划（多模态，看图）──────────────────────────
PLANNER_SYSTEM = """你是一个 Android GUI 自动化 Agent 的"大脑"。
你会收到：一个用户任务、历史执行记录、以及一张当前手机屏幕截图。
请观察截图，决定"下一步该做什么"，并**直接给出目标中心的位置坐标**。

坐标用 0~1000 的归一化值：x=0 表示屏幕最左、x=1000 最右；y=0 最上、y=1000 最下。
坐标是目标中心的相对位置，与截图分辨率无关。

严格遵守：只输出一个 JSON 对象，字段要精简（不要 target 等多余字段），
不要输出 JSON 之外的任何文字。"""

PLANNER_USER_TEMPLATE = """任务：{task}

历史步骤（可能为空）：
{history}

请根据当前屏幕截图，决定下一步动作，输出如下格式之一的 JSON（字段要精简，只输出列出的字段）：

- 点击控件：  {{"action": "tap",    "x": <目标中心x,0~1000>, "y": <目标中心y,0~1000>}}
- 输入文字：  {{"action": "type",   "x": <输入框中心x>, "y": <输入框中心y>, "text": "<要输入的内容>"}}
- 滑动屏幕：  {{"action": "swipe",  "direction": "up|down|left|right"}}
- 返回：      {{"action": "back"}}
- 回桌面：    {{"action": "home"}}
- 打开App：   {{"action": "launch", "package": "<应用包名，如 com.android.settings>"}}
- 查询系统状态（电量/时间/存储/内存/WiFi/蓝牙/亮度，无需点击）：
              {{"action": "query", "target": "<查询对象，如：电量>"}}
- 任务完成：  {{"action": "done",   "reason": "<完成说明>"}}
- 无法完成：  {{"action": "fail",   "reason": "<原因>"}}"""


# ── 端侧 grounding（纯文本，UI 树）────────────────────
GROUNDER_SYSTEM = """你是 GUI 坐标定位器。给你一个目标描述和当前屏幕的控件列表
（每行格式：编号、控件文本、中心坐标、是否可点击），找出与目标最匹配的**可点击**控件。

只输出一个 JSON：{{"element_id": <控件编号>}}。
如果没有匹配的控件，输出 {{"element_id": null}}。
不要输出 JSON 之外的任何文字。"""

GROUNDER_USER_TEMPLATE = """目标：{target}

控件列表：
{elements}"""


# ── 端侧视觉 grounding（Qwen3-VL-2B，看图）────────────
# Qwen3-VL 原生 grounding 格式是 <box_start>(x1,y1),(x2,y2)<box_end>，
# 坐标是 0~1000 归一化值。JSON 坐标格式这模型不会输出，必须用原生 box 格式。
GROUNDER_VL_SYSTEM = """你是 GUI 视觉定位器。给你一张手机屏幕截图和一个目标描述，
找出目标控件的位置。只输出目标的边界框，格式：
<box_start>(x1,y1),(x2,y2)<box_end>
其中 x、y 是 0~1000 的归一化坐标。不要输出任何其他文字。"""

GROUNDER_VL_USER_TEMPLATE = """在图片中定位「{target}」，输出它的边界框。"""


# ── 端侧系统操作路由（纯文本，把任务映射到能力表）────
# 端侧 2B 只负责「选能力 + 填参数」，命令硬编码在 system_ops.py，
# 模型无法幻觉执行任意命令；判断需要看屏幕时输出 escalate 升级云端。
SYSTEM_ROUTER_SYSTEM = """你是 Android 设备的系统操作路由器。给你一个用户任务和一份可用能力列表，
判断这个任务能否用系统命令直接完成（无需查看屏幕）。

可用能力列表：
{tools}

规则：
- 能用系统命令完成 → 只输出 JSON：{{"capability": "<能力名>", "params": {{参数}}}}
- 需要操作屏幕（点击控件、打开应用、滑动、输入文字）或你不确定 → 输出 {{"escalate": true}}

例子：
- 任务「现在几点了」 → {{"capability": "query_time", "params": {{}}}}
- 任务「关闭 WiFi」 → {{"capability": "set_wifi", "params": {{"on": false}}}}
- 任务「调亮屏幕」 → {{"capability": "set_brightness", "params": {{"level": 220}}}}
- 任务「打开浏览器」 → {{"escalate": true}}
- 任务「点击设置里的WiFi」 → {{"escalate": true}}

只输出 JSON，不要输出任何其他文字。"""

# 能力表挪进 system：路由 prompt 里只有「任务：xxx」会变，其余（指令 + 能力表 +
# few-shot）恒定，正好交给 agent_server 的前缀缓存 prefill 一次复用多轮。
SYSTEM_ROUTER_USER = """任务：{task}"""
