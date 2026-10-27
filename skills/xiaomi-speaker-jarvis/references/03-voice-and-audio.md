# 接通云转写与音箱播报

本指南对应公开包中的 [xiaomi_voice.py](../assets/runtime/src/home_jarvis/xiaomi_voice.py)、[speaker.py](../assets/runtime/src/home_jarvis/speaker.py)、[quiet_speaker.py](../assets/runtime/src/home_jarvis/quiet_speaker.py) 和 [speech.py](../assets/runtime/src/home_jarvis/speech.py)。当前适配器只接受 `xiaomi.wifispeaker.s12`。其他型号需要独立核验，不能直接套用这台 S12 的现场结论。

## 1. 确认实际输入与输出路径

目的是真正接通现有原厂设备，同时保留每一层的验证边界。

```text
小爱同学唤醒 → 原厂 ASR → 小米云会话记录 query
→ VoiceBridge → Core / MiniMax 原生工具 → Home Assistant
→ 持久化真实回执 → 局域网 legacy tts → 原厂播放器
```

Jarvis 收到的是云端文本。当前链路没有采集原始麦克风 PCM，没有取得设备 shell，也没有接管系统唤醒词。局域网传输 `tts` 命令不证明断网时仍能完成原厂语音合成。

唤醒后直接说问题或设备指令，不必增加“贾维斯”。`addressed_text()` 仍兼容句首“贾维斯”及 `Jarvis`，单独前缀或空文本不执行。语音来源由服务端固定为 `xiaomi-cloud`。Core 只接受会话的主 ASR 来源且 `final=True` 的输入，部分转写、另一个 ASR 来源不能重复执行同一句话。这里的云会话记录作为最终文本提交，不是实时 ASR 流。

云账号绑定不是声纹鉴权。音箱附近的人仍可能发送指令。HA 白名单、来源房间和权限应在启用语音前完成。

## 2. 先验证云记录身份与基线

目的为防止读错音箱、开机补执行历史问题，以及同一次转写多次触发动作。

1. 准备仓库外的 `0600` 私有文件。云配置只包含 `userId`、`serviceToken`、`deviceId`、`miotDID`、`hardware`。局域网配置的 `did` 必须与 `miotDID` 相同。不要把账号、token 或家庭设备标识写入公开示例。
2. 让 `XiaomiCloud.verify_device()` 核对目标。它要求目标 DID 唯一、云 `deviceID` 和 `hardware` 完全一致，并要求该账号下相同 `hardware` 只有一台。新增同型号设备后必须重新评估绑定方式。
3. 运行桥接后，等待 `status=ready` 再发出测试问题。第一次成功读取只建立历史基线，现有记录标记为 `baseline`，不会执行。
4. 检查新问题产生新的 `handled`。同一记录重复出现或内容被修订时，不应产生第二次模型、HA 或 TTS 请求。

读取器采用 `GET https://userprofile.mina.mi.com/device_profile/v2/conversation`，参数为 `source=dialogu`、`hardware`、当前毫秒 `timestamp`、`limit=2`，并携带目标设备 cookie。它只读取 `query`，不会把小爱 `answers` 当作用户输入。该非公开稳定协议可能变化，收到 HTTP 200 还要验证业务状态和响应结构。

桥接对每条记录执行这些检查。

| 检查 | 当前行为 | 未满足时 |
| --- | --- | --- |
| 启动后输入 | `record.time_ms > started_ms` | 忽略开机前记录 |
| 新鲜度 | 距当前时间不超过 60 秒 | 忽略过期记录及过期模型结果 |
| 未来时间 | 最多允许比当前时间快 5 秒 | 忽略异常未来记录 |
| 到达顺序 | 时间不早于已观察到的最新记录 | 忽略延迟到达的旧指令 |
| 持久化去重 | 对记录 ID 哈希，以 session 与事件 ID 唯一登记 | 相同 ID 的转写修订也不重执行 |
| 连续性 | 云故障后取消旧提案，恢复后重新建立基线 | 不补执行断线期间记录 |

同样的文字、不同的新记录 ID 是新的指令。需要做重复性测试时，复用相同 ID、session 和数据库。重新说一遍会创建新输入，不能用来证明去重。

## 3. 用前台桥接核验一次新鲜问题

先按 [参考运行时指南](07-reference-runtime.md) 安装，将下面路径替换为本机绝对路径。`read -r JARVIS_DID` 会等待输入；输入认证时使用的真实 DID 并回车，使云配置文件名与登录脚本的输出一致。下面命令会访问真实账号、模型和设备，应在目标家庭已授权范围内执行。先停止同一音箱的其他桥接进程。

```sh
JARVIS_RUNTIME="/absolute/path/to/assets/runtime"
JARVIS_PRIVATE="$HOME/.config/home-jarvis"
JARVIS_PYTHON="$JARVIS_RUNTIME/.venv/bin/python"
read -r JARVIS_DID

"$JARVIS_PYTHON" -m home_jarvis.xiaomi_voice \
  --cloud-config "$JARVIS_PRIVATE/xiaomi-cloud-$JARVIS_DID.json" \
  --speaker-config "$JARVIS_PRIVATE/speaker.json" \
  --home-config "$JARVIS_PRIVATE/home.json" \
  --model-config "$JARVIS_PRIVATE/minimax.json" \
  --db "$JARVIS_PRIVATE/xiaomi-voice.sqlite3" \
  --quiet-native-replies --reply-volume 16
```

成功证据分开记录。

1. `ready` 显示 `source=xiaomi-cloud`、`input_mode=all_transcripts`、`command_prefix=null`、预期模型及 `audio_mode=quiet_native_replies`。这证明桥接完成初始化。
2. 唤醒后说一条没有家居副作用的问题，例如“一加一等于几”。确认它产生新的回执，且只有一次最终播报请求。
3. 记录 `speech.status=accepted`。这证明 TTS 接收回包，不证明已经听到。
4. 读取播放器从 `status=1` 返回 `status=0`，并确认最后 `volume=0`。这证明观察到播放状态结束。
5. 请现场使用者独立确认听到了一次预期回答。只有这一步才能记录“现场听到”。

静音模式的云转写轮询间隔为 3 秒，未启用静音模式时为 1 秒。云错误后等待 30 秒再创建新读取器。不要把这些间隔描述为端到端延迟保证。

按 `Ctrl-C` 或发送 `SIGTERM` 退出会执行清理。正常退出恢复的是 `--reply-volume`，默认 16，并非启动前捕获的任意音量。若出现 `audio_restore_failed`，需要单独核验音量，不能宣称已恢复。

## 4. 保留 legacy TTS 的单次发送约束

目的为兼容已验证的 S12 方法，同时避免超时后重复播报。

| LAN 方法 | 参数 | 当前用途 |
| --- | --- | --- |
| `miIO.info` | `[]` | 每次操作前核对型号和 MAC |
| `tts` | `[text]` | 发送 1 至 500 字符的非空 UTF-8 文本 |
| `play_pause` | `[]` | 请求暂停；该 S12 路径是 pause，不是播放状态 toggle |

不要因为 MIoT 方法超时，就判定 legacy 方法也不可用。也不要把云端 `remote/ubus` 方法名直接发送到本地 miIO UDP。

`XiaomiSpeaker` 复用 Device 对象，但必须保持 `lazy_discover=False`。源项目曾出现短时成功、常驻空闲后暂停与 TTS 失败的故障。对 `python-miio==0.5.12` 的机制复现发现，默认缓存握手时间戳没有累计真实空闲时长；人为将缓存调旧后查询失败，同一对象刷新握手后恢复。公开适配器因此在每次请求前重新发现设备。库的发现阶段仍可能使用默认 5 秒超时，不等于命令配置的 3 秒超时。

身份查询、TTS、暂停都使用 `retry_count=0`。网络超时可能发生在设备已执行之后，所以结果记为 `uncertain`，不能自动重发，也不增加云 TTS 自动回退。Device 的起始请求 ID 使用随机值，降低 S12 请求 ID 去重与库 ID 环绕导致的冲突。

`deliver_speech()` 先在 SQLite 的 `cli_speech` 登记 `uncertain`，再发送。重复的 session 与 utterance ID 返回持久化回执，并标记 `replayed=true`，不会再次出声。不要删除失败记录来“修复”无声。新测试必须是用户新授权的新输入。

## 5. 核验真正的零音量与起播窗口

目的为在待机时抑制原生回答，并在 Jarvis 回复期间短暂恢复音量。

当前 `_PlayerClient` 通过小米云 `https://api2.mina.mi.com/remote/ubus` 调用 `mediaplayer`。

| 操作 | 方法与消息 | 验收条件 |
| --- | --- | --- |
| 读播放状态 | `player_get_play_status`, `{"media":"app_ios"}` | `status` 只能为 0 或 1，`volume` 为 0 至 100 |
| 设共享音量 | `player_set_volume`, `{"media":"airplay","volume":0}` | 随后读回 0 |
| 恢复播报音量 | 同一 setter，`volume=16` 或配置值 | 随后读回目标值 |

源项目在该 S12 上实测，普通 `app_ios` 设置路径请求 0 会读回 1，`airplay` 路径才读回 0。`app_ios` 仍是当前状态查询参数，不要把读取和设置两条路径一并替换。这里只使用 `airplay` 这个消息字段，没有启动 AirPlay 音频流。

先完成零音量现场校准，再启用长期静音。使设备读回 0，唤醒“小爱同学”并说一个问题，同时确认云端确有新转写；现场确认原生答案没有声音。最后恢复并读回预定音量。源项目已在一台 S12 上验证零音量仍可唤醒和 ASR，但这不是所有固件的保证。

`QuietSpeaker` 的每次回复按以下顺序进行。

1. 保持音量 0。
2. 请求 LAN `play_pause`。
3. 最多等待 5 秒，直到状态同时为 `volume=0`、`status=0`。不能跳过这一步，否则恢复音量时旧原生回答可能继续出声。
4. 设置并读回播报音量，默认 16，状态读回等待上限为 3 秒。
5. 检查取消标记后发送一次 TTS，进入 `starting`。
6. 等待实际观察到 `status=1` 才进入 `playing`。刚收到 TTS 后的 `status=0` 不能当作已播完。
7. 从 `playing` 观察到 `status=0` 后归零，回到 `idle`。

未观察到起播的期限是 8 秒。从发送 TTS 起整个开放窗口上限为 120 秒。活动期每 1 秒检查一次，待机每 5 秒检查一次。超时或状态异常进入 `fault`，尝试归零，保留错误，不重发 TTS。很短的音频可能在两次查询间播完，结果会按起播超时处理。

适配器的锁和单个 watcher 保护同一条播放状态。新输入、停止命令和云连续性中断会取消旧模型提案，并尝试暂停和归零。取消不能撤销已发出的 HA 请求。不要为同一音箱开第二个 QuietSpeaker 或第二套播报服务；数据库锁只阻止同一数据库的重复桥接，无法阻止使用不同数据库的另一个进程。

## 6. 明确静音模式的产品限制

小爱原生 NLU 和可能的设备动作仍然运行。共享音量归零只抑制声音，不能防止小爱和 Jarvis 同时控制同一设备。测试家庭需要明确两者各自可执行的范围。

Jarvis 播报窗口内共享音量非零。此时再次唤醒，原生回答可能混入声音。播放器状态没有来源 ID，代码无法证明观察到的播放一定来自 Jarvis。该模式不承诺物理打断、AEC 或全双工。

静音 watcher 会把手动调高的共享音量重新归零，因此也影响原生音乐和物理音量键。Mac 休眠、强杀进程或云端失联会影响静音与恢复。正常退出清理不能覆盖断电和 `SIGKILL`。

这些限制决定下一步验收范围。稳定性问题先按 [故障排查](06-troubleshooting.md) 分层定位。若目标改成完全关闭小米云、替换系统唤醒词或取得本地 PCM，需要单独设计硬件与固件方案，不能把本指南的 token 接入当作已经获得 root。
