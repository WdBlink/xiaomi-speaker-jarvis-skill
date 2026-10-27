# 按证据定位失败层

先找到最后一个确定成功的环节，再处理下一环。保留原始业务回执，不删除数据库、不重新播放旧事件，也不把失败升级成另一条自动执行路线。命令变量沿用 [运维指南](05-operations-and-validation.md)，安装与路径见 [参考运行时指南](07-reference-runtime.md)。

## 1. 先画出本次故障的证据链

对一个具体的新输入，逐项记录以下事实。

1. 原厂唤醒是否发生，云端是否出现该次 `query`。
2. 桥接是否已经 `ready`，该记录是否新鲜且不是历史基线。
3. 是否有 `handled`，回执为 `committed`、`model_failed`、`model_cancelled` 或其他状态。
4. 是否产生 HA 动作，逐项是 `applied`、`failed` 还是 `uncertain`。
5. 是否出现 `native_pause`、`speech.status` 和音频错误。
6. 是否观察到播放起止与最终音量，现场是否真正听到。

只保存本轮所需证据。对外报告使用脱敏的事件别名，不复制家庭 IP、MAC、DID、账号、token、转写原文或用户目录。HTTP 状态、业务错误码、方法名、状态变化和错误类别通常足以定位问题。

## 2. 听得到唤醒声，但没有云转写

先验证云凭据和目标绑定。先按运行时指南设置 `JARVIS_DID` 为登录使用的真实 DID；云文件名必须与登录输出一致。用读取器检查是否能取得记录，只输出条数和年龄，不打印内容或设备标识。

```sh
JARVIS_CLOUD_CONFIG="$JARVIS_PRIVATE/xiaomi-cloud-${JARVIS_DID:?先设置登录时的真实DID}.json" \
  "$JARVIS_PYTHON" - <<'PY'
import json
import os
import time
from home_jarvis.xiaomi_cloud import XiaomiCloud

cloud = XiaomiCloud(os.environ["JARVIS_CLOUD_CONFIG"])
records = cloud.latest_records()
now_ms = int(time.time() * 1000)
print(json.dumps({
    "record_count": len(records),
    "ages_ms": [now_ms - record.time_ms for record in records],
}))
PY
```

成功仅说明能读取当前云记录。需要现场发出新问题，再确认新记录时间发生变化，才能证明本次 ASR 到达云端。不要把老的两条记录当作刚完成的转写。

| 现象 | 检查与处理 | 完成条件 |
| --- | --- | --- |
| 凭据过期、HTTP 认证失败 | 用已授权的官方登录流程重新获取该目标凭据，保留失败时的旧文件 | 新配置通过身份校验，桥接重新建立基线 |
| `Configured Xiaomi speaker was not uniquely found` | 核对账号是否包含预期 DID，不自动挑选第一台 | 唯一目标与本地配置一致 |
| `cannot uniquely bind this hardware model` | 检查账号是否多了一台同 hardware 音箱 | 解决绑定歧义后再启用，不放宽检查来猜目标 |
| `response has an invalid shape` | 保留脱敏响应结构与业务码，核对当前云接口 | 按明确协议解析成功，不绕过响应校验 |
| 没有新记录，旧记录一直存在 | 检查音箱联网、账号、真实发声与客户端时钟 | 观察到新问题对应的新记录 |

`mibrain/nlp_result_get` 在源 S12 接入中曾返回业务码 1001，读取器改用当前 conversation 接口。不能因此把任意固件的 1001 都解释为相同根因，也不能把失败接口的名字换成本地 UDP 命令。

## 3. 云端有记录，Jarvis 没有处理

核对 `ready` 的 `input_mode=all_transcripts` 和 `command_prefix=null`。当前版本不要求“贾维斯”前缀，不能让用户多说前缀来掩盖桥接没有运行。

依次检查以下条件。

1. 记录是否早于本次启动基线。第一次读取故意忽略历史，服务恢复后也不会补执行停机时的命令。
2. 记录是否超过 60 秒，是否比最新已观察记录更旧，或比本机时间快超过 5 秒。
3. 记录 ID 是否已持久化。相同 ID 的文字修订不重新执行；同文字新 ID 才是新输入。
4. Core 的 `Utterance.final` 是否为布尔 `True`，来源是否等于 session 的主来源 `xiaomi-cloud`。
5. 是否有更新的输入、停止指令、云故障或服务关闭取消了旧模型提案。

`model_cancelled` 表示旧结果被丢弃，不能为使它发声而重新提交旧事件。若需要再次执行，先解释当前状态，再由新的明确输入形成新事件。

## 4. 模型返回 `model_failed`

先区分网络、响应结构、工具参数校验和时效问题。查看同一时间附近的脱敏 `model_error.reason`。当前 `_CancellablePlanner` 只记录受控 `PlannerError` 文本，未知异常只记类型名；不要改为打印完整请求、API key、模型思考或 HA 凭据。

源项目首次启用原生工具版本后，两条真实“打开客厅窗帘”请求曾失败。旧日志没有保留异常原因。后来用当前状态，以及两帘关闭的只读快照回放，同一句话能生成正确动作。这只能证明后续回放成功，历史根因仍未知。

失败回执中的 `model_calls=1` 不能当作精确 HTTP 调用计数。当前 Core 在失败分支可能保留初始计数，第二回合失败时会少报。统计请求次数应使用脱敏传输级计数或服务方记录，并说明观测范围。

若模型探针生成正确计划，但实机请求失败，继续检查 Core 校验、过期或取消、HA 预检和 Outbox 状态。不要把只读计划 probe 当成动作已执行，也不要自动退回旧规则解析器或替换目标设备。

## 5. 家居动作完成，但音箱没声音

先保留 `results[]` 的真实 HA 回执。播报失败不应把已经 `applied` 的动作改成失败，更不能再次控制设备来重试音频。

按以下顺序定位。

1. `speech.status=deferred` 表示家居动作仍在执行，或模型还没有最终回执。等待原处理流程，不另开播报器。
2. `speech.status=suppressed` 表示模型提案被取消。不得补播旧答复。
3. `speech.status=rejected` 表示回复不适合 TTS，检查空文本和长度上限。
4. `speech.status=uncertain` 表示登记、发送或接收结果未确认。保持原回执，不自动重发。
5. `speech.status=accepted` 后仍无声，检查音量、起播状态、实际设备和现场听音。接收回包不是声学证据。

当前 TTS 最大长度为 500 字符。源项目曾在 31 个家居动作的逐项回执超过上限时无声。现有 `speech.py` 从持久化的最终真实回执统计 `applied`、`failed`、`uncertain`，生成短摘要，例如“本次31个家居动作，23个已确认完成，4个失败，4个结果不确定。不会自动重试。”完整逐项回执仍留在数据库。

该摘要只用于全部为家居动作、且状态在上述三类中的超长结果。其他超长内容仍拒绝播报，不截断成可能改变意思的句子，也不让模型重新编造完成报告。`test_speaker_cli.py` 使用虚构批次验证此问题，不需要真实操作 31 台设备。

## 6. 短时可用，常驻空闲后不回复

如果云转写和模型结果正常，却在 `native_pause` 或 TTS 前失败，先检查 LAN 层。源项目已经复现过 `python-miio==0.5.12` 的握手时间戳缓存问题。

1. 核对公开 [speaker.py](../assets/runtime/src/home_jarvis/speaker.py) 中构造 `Device` 时保留 `lazy_discover=False`。
2. 核对 `miIO.info`、`tts` 和 `play_pause` 都保持 `retry_count=0`。
3. 运行 `test_speaker.py` 中的协议缓存回归，确认实际安装的协议库参与测试，未被跳过。
4. 在已授权的现场，先做新鲜的只读身份查询，区分设备离线、身份不符和缓存老化。人工调旧缓存属于专项受控机制实验，不是常规修复命令。
5. 修复后用新的语音输入完成一次播放起止和听音验证。旧的 `uncertain` 播报继续保留，不补发。

不要通过增加 miIO 重试次数或云 TTS 回退掩盖这个问题。设备可能已经执行但回包丢失，自动重发会产生重复声音。源项目的缓存实验是人为老化 14 分钟，并非实测等待了 14 分钟；协议测试覆盖模拟空闲 24 小时，也不是设备连续跑满一天的证据。

## 7. 音量设 0 却读回 1，或原生答案仍出声

先只读当前播放器状态。以下探针会访问小米云，但不会改变音量或启动 QuietSpeaker。

```sh
JARVIS_CLOUD_CONFIG="$JARVIS_PRIVATE/xiaomi-cloud-${JARVIS_DID:?先设置登录时的真实DID}.json" \
  "$JARVIS_PYTHON" - <<'PY'
import json
import os
from home_jarvis.quiet_speaker import _PlayerClient

client = _PlayerClient(os.environ["JARVIS_CLOUD_CONFIG"])
client.verify_device()
state = client.get_status()
print(json.dumps({"status": state.status, "volume": state.volume}))
PY
```

`_PlayerClient` 是当前参考实现的内部接口，使用前以随包源码为准。不要为了只读状态实例化 `QuietSpeaker`，它初始化时会真实设零音量并启动 watcher。

| 现象 | 原因候选与检查 | 下一步 |
| --- | --- | --- |
| setter 请求 0，读回 1 | 使用普通音量分支 | 核对 `player_set_volume` 消息为 `media=airplay` |
| 待机为 0，播报窗口听到原生回答 | 共享音量在 Jarvis 回复期间恢复；原生 NLU 仍工作 | 记录时间与状态，承认窗口限制，不宣称关闭了原生回答系统 |
| 一设 16 很快又归零 | 仍有 QuietSpeaker watcher 或第二个桥接 | 清点目标音箱的服务与测试进程，只保留一套控制 |
| `starting` 后立即静音 | 错把起播前 `idle` 当结束，或状态读取故障 | 核对等待 `playing` 的状态机和 8 秒起播窗口 |
| 很短的回复被记为启播超时 | 1 秒状态轮询可能漏掉整段播放 | 保留超时和最终音量证据，不能据状态单独推断是否听到 |
| `fault` 后出现静音失败 | 云控制不可用，实际音量未知 | 现场检查并在可达后恢复；不报告静音成功 |

测试零音量时，如果云读取失败导致探针先退出并恢复音量，使用者之后说话不属于“音量 0 的 ASR 测试”。必须对齐发声时间、状态读回和恢复时间。

## 8. 服务启动失败或出现两次回答

`This voice database already has a running bridge` 表示当前数据库已有进程持锁。先确认预期服务，正常停止旧实例并等待 PID 退出；不要换数据库、删锁文件或并行启动来绕过它。

若同一句听到两次，分别检查以下来源。

1. 一个声音是否来自原生小爱，另一个来自 Jarvis。
2. 是否同一音箱存在不同数据库的两套服务。文件锁不覆盖这种情况。
3. 是否有人重说，形成不同记录 ID；这与重复投递同一 ID 不同。
4. `speech.replayed=true` 是复用回执而非重新播放。核对实际发送次数，不把日志重读误当成 TTS 重发。
5. 临时测试脚本是否没有退出，或正在关闭时恢复音量干扰常驻 watcher。

按 [重载步骤](05-operations-and-validation.md#4-重载时先等待旧-pid-退出) 管理服务。不要仅凭 `bootstrap` 成功就宣布恢复。需要新 `ready`、一个新问题的回执、最终音量与现场确认。

## 9. 无法恢复时留下可继续的证据

停止会继续制造副作用的测试，保留持久化回执与可复现步骤。记录最后确认的服务 PID、加载版本、输入方式、最后成功阶段、错误类别、HA 状态和音量状态。标注哪些是读回、哪些是现场确认、哪些尚不确定。

如果必须等待账号重新认证、设备联网或使用者现场听音，只暂停依赖该条件的验证。完成可独立执行的离线检查、脱敏和进程清点。报告真实未完成项，不用“已修复”覆盖未知根因，也不要留下无人管理的轮询、播报重试或家居动作。
