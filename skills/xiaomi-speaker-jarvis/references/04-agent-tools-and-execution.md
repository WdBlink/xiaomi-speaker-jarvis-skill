# 原生工具与真实执行

本章对应 `assets/runtime/src/home_jarvis/{planner,core,home,xiaomi_voice,speech}.py`。阅读它以定位“必须指定准确名称”“没有分析能力”“模型说成功但设备没动”等故障。这里复用用户配置的模型；参考适配器已验证 MiniMax-M3，列入允许配置的 M3.1-Flash 不代表已在同一硬件场景实测。

## 1. 先证明问题在哪一层

沿一次输入 ID 串起转写、模型、outbox、HA 回读和播报，不只看最终一句话。

原案例中，“把客厅的灯关掉”进入模型后仍被改写为“请明确房间、设备名称和支持的动作”。目录实际存在三组客厅灯；旧本地解析器只认“客厅所有灯”等词形，Core 又要求模型动作与这套解析器的结果完全一致。

于是模型并没有最终的设备选择权。先后增加“的”“里的”、窗帘类别、时间委托正则，仍会在下一个自然表达失败。应该移除这层二次语义审批，而不是持续扩大同义词表。

| 层 | 应该判断 | 不应该承担 |
| --- | --- | --- |
| 模型 | 本句意图、房间和设备类别、纠正与否定、建议还是立即操作 | 生成访问令牌、扩大设备权限、编造传感器读数 |
| 工具参数解析 | JSON 字段、类型、长度、重复实体、允许动作 | 用原句正则再次证明模型是否理解 |
| 执行器 | 用户/来源身份、实体归属与能力、时效、持久化、取消、请求结果 | 相信模型“已完成”的文字 |
| 播报 | 传达实际成功、失败或不确定 | 把 accepted 当现场听到，掩盖部分失败 |

## 2. 建立可理解的设备目录

每个可控实体提供 `entity_id`、友好名称、房间 ID/名称、设备别名、房间别名、domain、允许动作。不要把 HA 全量状态和诊断属性直接给模型，也不要仅提供难懂的实体 ID。

示例目录是虚构的：

```json
[
  {"entity_id":"light.example_ceiling","name":"客厅吸顶灯","room_id":"living_room","room":"客厅","domain":"light","aliases":["顶灯"],"room_aliases":["起居室"],"actions":["on","off"]},
  {"entity_id":"light.example_strip","name":"客厅灯带","room_id":"living_room","room":"客厅","domain":"light","aliases":[],"room_aliases":["起居室"],"actions":["on","off"]},
  {"entity_id":"cover.example_sheer","name":"客厅纱帘","room_id":"living_room","room":"客厅","domain":"cover","aliases":[],"room_aliases":["起居室"],"actions":["open","close","stop","set_position"]}
]
```

“客厅灯”通常选该房间灯类实体；明确“吸顶灯”只选它。“客厅窗帘”可包括纱帘和遮阳帘；“窗帘灯”虽然含“窗帘”二字，domain 仍是灯。具体房间映射必须从设备证据建立，不能为了避免追问猜测硬件身份。

来源房间来自绑定会话。需要依据当前房间作省略推断时，模型可从 `query_home` 取得它；不要把音箱所在房间误做禁止跨房间控制的硬约束。

## 3. 暴露两个原生 function tools

参考实现直接使用提供商的 `tools`，识别 `finish_reason: tool_calls`；不是让模型在普通文本里伪造 `{kind: home}` JSON。

`query_home({})` 只读返回 `catalog` 和 `snapshot`：

```json
{
  "observed_at": 1700000000,
  "local_time": "2023-11-15T06:13:20+08:00",
  "timezone": "Asia/Shanghai",
  "room_id": "living_room",
  "devices": [
    {"entity_id":"cover.example_sheer","state":"closed","position":0,"supported_features":15}
  ],
  "sun_state": "below_horizon",
  "observation_error": null
}
```

`control_home` 提交一个动作批：

```json
{
  "actions": [
    {"entity_id":"light.example_ceiling","action":"off"},
    {"entity_id":"light.example_strip","action":"off"}
  ],
  "reason": "用户要求关闭客厅灯，选择该房间两组灯"
}
```

`owner_id`、HA 服务 URL 和实体所属房间不由模型传入。房间从本地实体目录绑定；POST 服务从 domain/action 固定映射。`position` 只在 `set_position` 中出现，必须是真整数 0–100；布尔值不接受。0 全闭、100 全开、1 是仅开 1%，不是“基本打开”。未知 entity、额外字段、重复 JSON key、重复设备、非法动作或超出目录总数的批次全部拒绝。

一次输入最多一次只读查询、最多两次模型请求、一个终止规划的控制批。第二次请求移除查询工具；收到写工具后回到 Core 持久化执行。这是当前版本的有界实现，不是通用自主任务循环。

## 4. 正确往返工具消息

简化协议：

```text
user: 把客厅的灯关掉
assistant: tool_calls(query_home, {})
tool: 对应 tool_call_id + 脱敏目录和新鲜快照
assistant: tool_calls(control_home, actions=[...])
Core: 校验整批 → 保存 outbox → HA 执行并回读 → 实际回执播报
```

查询之后必须把完整 assistant message 连同 `tool_calls`、相同 `tool_call_id` 和提供商需要的 reasoning 字段回送模型。不要只回送 content，也不要把思考内容播给用户。参考客户端识别官方响应里的 `name`、`audio_content`、工具 `index=0` 等元数据；不因无害的已知字段拒绝整个回合。

MiniMax-M3 示例使用 `thinking: {"type":"disabled"}` 以控制交互延迟；M3.1-Flash 不接受 disabled，适配器使用低 reasoning effort。实际参数以当前官方文档和只读 API 探针为准。API 失败不自动重试。详细协议参见 [MiniMax 工具调用](https://platform.minimax.io/docs/guides/text-m3-function-call) 和 [OpenAI 格式兼容说明](https://platform.minimax.io/docs/api-reference/text-openai-api)。

执行后不再请求模型改写成功结果。不要对外宣称“模型已经读取写入结果并自动修复”；当前真实回执由宿主直接生成。

## 5. 给分析提供事实，而不是特例正则

`query_home` 从 HA `/api/config`、`/api/states` 提取时区、允许设备状态/位置/能力和 `sun.sun`，不发送令牌、地址、坐标或其他实体属性。缺失、重复、不可用或非法状态记 unknown；错误返回脱敏原因。

告诉模型：

- 只问“应该关哪个”或“先给建议”时分析即可，不执行。
- 明确“你判断后帮我关上需要关闭的窗帘”时，可基于所见事实选择目标，也可判断无需动作，不要求重复许可。
- 夜间隐私可以作为一般理由，不要假装读取了人员、室内照度、天气或窗户朝向。
- 时间和状态并不构成“每晚全部关闭”的固定策略。定时或未来条件执行需要独立的调度能力；不能偷偷改成现在执行。
- 同句改口采用最终明确意图；否定动作不调用控制工具。语义准确性仍取决于模型，权限校验不能证明意图理解无误。

“根据时间关哪扇”不应被降格为仅识别包含“根据、时间、判断、关上”的新正则通道。先检查模型拿到了事实，再检查其原生工具选择。

## 6. 复用持久化执行，不把写权限塞进模型客户端

SQLite 生命周期：

1. 校验 `(session_id, utterance_id, owner, primary_source, final)`，重复输入直接取已保存回执。
2. 登记 `model_pending`，释放写锁后请求模型和查询 HA。
3. 回到事务，确认提案未被取消、会话身份未变、请求与快照仍有效，并一次性授权整批动作。
4. 保存整批 outbox `pending` 和已提交回执，再逐项发送。
5. 每项发送前登记 `in_flight`；预读 HA 状态与能力后，在 POST 前再检查取消。
6. 发送一次服务请求，查询真实状态，保存 `applied`、`failed` 或 `uncertain`。同输入 ID 再提交不重放 pending、in_flight 或 uncertain。

状态快照有效期 45 秒，请求有效期 60 秒。所有模型动作都使用这些检查，不只为某种窗帘委托特例启用。新的语音会取消旧提案及旧播报；已经发出的请求只能观察结果，不能当作从未发生。

Home Assistant 的服务返回不等于动作完成：灯应读回 on/off；窗帘应按目标状态和位置确认。完整域/服务与返回语义参见 [HA REST API](https://developers.home-assistant.io/docs/api/rest/)。

## 7. 验证理解、执行和反馈，各自留证

先做不执行的模型探针，记录原句、工具名、参数和返回类别，不记录密钥或 reasoning。再用已授权的真实设备验收；不要拿假的模型输出证明理解能力。

| 输入/事件 | 检查什么 |
| --- | --- |
| 把客厅的灯关掉 | 按目录选房间灯类；不要求逐字设备名 |
| 只关客厅吸顶灯 | 只选指定实体 |
| 不要关客厅灯 | 不生成控制动作 |
| 先说说应该关哪个窗帘 | 查询事实、给建议、不写入 |
| 根据现在情况选择并关闭需要关的窗帘 | 基于快照作选择，或明确无需动作 |
| 十分钟后关灯 | 不把未来动作当立即控制，也不假称已创建计时器 |
| 第二项参数非法 | 整批拒绝，第一项也不发送 |
| 第一项执行后新语音到来 | 尚未发出的项取消，已执行项保留真实回执 |
| 同输入 ID 再提交 | 无第二次模型、HA、TTS 请求 |
| 请求超时但设备可能已动作 | uncertain，不自动补发 |
| 全屋多设备回执超过 500 字 | 按真实成功/失败/不确定数量摘要；完整明细仍留库 |

原实践最终源工程有 255 项自动检查，实测“客厅的灯”选择三组灯并确认关闭。它不是 255 个实机场景，也不是对所有中文表达的证明。公开包的实际检查数量在仓库验收记录中单独说明。

两次生产窗帘请求曾失败，原异常被吞，后来同句回放与完整入口正常，尚未归因。现有 `model_error` 记录脱敏 PlannerError 或异常类型；失败回执的 `model_calls` 仍可能少报第二次请求，不应当作费用统计。排障时重建发生当时的状态快照；设备已打开后的 query→reply 成功不能代替原来的 query→control 验证。
