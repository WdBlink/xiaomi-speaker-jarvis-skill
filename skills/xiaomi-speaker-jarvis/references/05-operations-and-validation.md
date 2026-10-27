# 部署、重载与分层验收

本指南适用于 macOS 当前用户的 LaunchAgent。先完成 [参考运行时安装](07-reference-runtime.md)、私有配置和 [前台语音验证](03-voice-and-audio.md)。常驻依赖 Mac 在线且未休眠，小米云会话有效，模型服务和 HA 可访问。当前代码不自动续期账号凭据。

## 1. 先定义证据层级

每次验收同时记录输入方式和结果层级。合成输入、真实 API、状态读回和现场听音是不同证据。

| 观察 | 能证明什么 | 不能据此声称什么 |
| --- | --- | --- |
| Mock 返回 `mock_applied` | 本地路由、持久化或去重逻辑按预期运行 | 真实 HA 已调用或设备已动作 |
| 模型只读 probe 生成 `control_home` 参数 | 在给定目录和快照下生成了一个计划 | Core 已执行、设备已动作 |
| 真实 HA 结果 `applied` | 运行时按该动作的读回规则确认目标状态 | 用户意图一定理解正确，或所有物理状态均已现场观察 |
| HA 结果 `uncertain` | 发送或目标状态确认存在不确定性 | 动作没有执行，或可以安全重发 |
| `speech.status=accepted` | 音箱返回预期接收回包 | TTS 已听到或播放已完成 |
| `starting → playing → idle`，最后音量 0 | 已观察到播放器状态变化及归零 | 声音必然来自 Jarvis，或现场声学效果合格 |
| 使用者明确确认听到一次预期回答 | 该次现场语音链路可听且没有多次回答 | 所有问法、所有型号或多日连续运行已通过 |

不要把合成 `ConversationRecord` 注入 `xiaomi_voice.main` 写成现场唤醒测试。不要把源项目曾通过的 255 项检查写成本公开包的新测试结果。公开包需要独立安装、检查和验收，测试数以当次真实输出为准。

## 2. 运行不接触硬件的检查

目的为先检查打包内容和回归行为，避免用真实家居动作代替单元测试。

```sh
JARVIS_RUNTIME="/absolute/path/to/assets/runtime"
JARVIS_PYTHON="$JARVIS_RUNTIME/.venv/bin/python"

cd "$JARVIS_RUNTIME"
"$JARVIS_PYTHON" -m unittest discover -s tests -v
"$JARVIS_PYTHON" -m home_jarvis
"$JARVIS_PYTHON" -m home_jarvis.xiaomi_voice --help
```

无参数 `home_jarvis` 使用临时数据库和 Mock。它输出 `mode=mock`，不能计入真实 HA 验收。`--help` 只检查入口及参数。单元测试使用替身，不需要填入私有凭据。

单独排查某层时，可以运行一个测试文件。

```sh
"$JARVIS_PYTHON" -m unittest discover -s tests -p 'test_speaker.py' -v
"$JARVIS_PYTHON" -m unittest discover -s tests -p 'test_quiet_speaker.py' -v
"$JARVIS_PYTHON" -m unittest discover -s tests -p 'test_xiaomi_voice.py' -v
"$JARVIS_PYTHON" -m unittest discover -s tests -p 'test_speaker_cli.py' -v
```

分别关注握手缓存刷新与单次发送、静音状态机与退出恢复、基线与取消、持久化播报去重与大批次摘要。协议库回归检查需要已安装 `python-miio==0.5.12`；看到相关测试跳过时，不能称该机制已经验证。

## 3. 准备 LaunchAgent 的具体配置

使用 [参数化 LaunchAgent 模板](../assets/runtime/config-examples/launchd.example.plist)。将模板复制到本机 `~/Library/LaunchAgents/`，再替换占位值。不要直接加载未填写的模板。

1. `ProgramArguments` 的 Python、配置、SQLite 都写绝对路径。使用已安装运行时的虚拟环境 Python，不依赖交互终端的 `PATH` 或当前目录。
2. `--cloud-config`、`--speaker-config`、`--home-config`、`--model-config` 指向仓库外的私有文件。命令行仅含路径，不能内联 token 或 API key。
3. 保留 `--quiet-native-replies` 和已现场校准的 `--reply-volume`。默认 16 只是源 S12 的已用设置，不能替其他音箱完成音量校准。
4. `--db` 必须为绝对持久化路径。沿用同一设备已有数据库，不为重载创建新数据库，不删除 SQLite 的 WAL、锁文件或失败回执来绕过问题。
5. 标准输出与错误日志位于仓库外的私有目录。模型回复、设备名称、DID 和转写可能出现在日志中，不能直接提交到公开仓库。
6. 检查模板的工作目录、日志父目录和凭据所有者。LaunchAgent 使用当前用户身份，不能通过换用户运行绕过 `0600` 检查。

使用本机参数执行语法检查。

```sh
JARVIS_LABEL="com.example.home-jarvis.xiaomi-voice"
JARVIS_PLIST="$HOME/Library/LaunchAgents/$JARVIS_LABEL.plist"
JARVIS_PRIVATE="/absolute/path/to/private/home-jarvis"
JARVIS_DB="$JARVIS_PRIVATE/xiaomi-voice.sqlite3"
JARVIS_DOMAIN="gui/$(/usr/bin/id -u)"

/usr/bin/plutil -lint "$JARVIS_PLIST"
```

`JARVIS_LABEL` 必须与已填写 plist 的 `Label` 完全相同。后续系统管理命令使用 `/bin/launchctl`、`/usr/bin/plutil` 等绝对路径，不假定服务拥有终端环境。

## 4. 重载时先等待旧 PID 退出

目的为让旧进程完成模型取消、watcher 关闭、音量恢复和数据库锁释放，避免新旧静音控制相互覆盖。

1. 记录当前 plist、运行时版本及本次变更。保存旧 plist 副本用于回退，例如 `$JARVIS_PLIST.previous`。核对它确实对应旧的可运行参数。
2. 在没有新用户输入的窗口记录当前 PID 和业务表计数。
3. 执行 `bootout`。
4. 反复只读检查记录的旧 PID，直到它已退出，才运行 `bootstrap`。不要把 `bootout` 返回当作进程已经完成 `finally`。
5. 检查新 PID、`ready`、音量和错误日志。注册成功只是开始，不能代替就绪验证。

```sh
/bin/launchctl print "$JARVIS_DOMAIN/$JARVIS_LABEL"
```

从输出记录旧 PID，设置为 `JARVIS_OLD_PID`。不要按宽泛的 `python` 进程名操作别的任务。

```sh
JARVIS_OLD_PID="EXAMPLE_OLD_PID"
/bin/launchctl bootout "$JARVIS_DOMAIN/$JARVIS_LABEL"
/bin/ps -p "$JARVIS_OLD_PID" -o pid=,command=
```

将 `EXAMPLE_OLD_PID` 替换为刚记录的数字。若 `ps` 仍显示旧桥接，等待它完成并复查。持续不退出时，查看该进程的错误和待完成请求，暂停加载新进程。强杀会跳过恢复音量，不是常规重载步骤。

旧 PID 已退出后再执行以下命令。

```sh
/bin/launchctl bootstrap "$JARVIS_DOMAIN" "$JARVIS_PLIST"
/bin/launchctl print "$JARVIS_DOMAIN/$JARVIS_LABEL"
```

若 `bootstrap` 失败，先用 `launchctl print` 判定是否已有残留注册，检查 `plutil`、路径、权限和旧进程状态。不要循环盲目重试。只有确认失败的新作业已卸载、其 PID 已退出后，才恢复保存的旧 plist 并 `bootstrap` 旧配置。回退也要等待新的 `ready`，核对音量，保留失败证据。源项目曾首次 `bootstrap` 失败，等待旧进程及残留作业退出后，再注册同一配置成功，没有回滚参数。先确认进程生命周期，再决定是否需要修改配置。

正常 `bootout` 会触发清理并恢复播报音量。暂停服务之后，小爱可能恢复可听状态。重启后的新静音服务应重新读回 0。

## 5. 验证重启没有补执行历史

目的为避免历史会话、旧模型结果或未确认播报在恢复时重放。运行下面只读查询，不修改业务表。

```sh
/usr/bin/sqlite3 -readonly "$JARVIS_DB" \
  "SELECT 'submissions', count(*) FROM submissions;
   SELECT 'home_outbox', count(*) FROM home_outbox;
   SELECT 'cli_speech', count(*) FROM cli_speech;
   SELECT state, count(*) FROM home_outbox GROUP BY state;"
```

只对已运行且包含这些表的数据库使用该查询。首次启动尚无 `cli_speech` 时，记录表尚未建立，不为获取数字而创建表。

在无人发出新输入的前提下，对比卸载前和重新 `ready` 后的 `submissions`、`home_outbox`、`cli_speech` 行数及动作状态。重载本身不应新增提交、动作或播报。`voice_seen` 可能因新基线登记而增长，这不等于动作重放。

同时确认没有新增 `handled` 家居回执，没有新的 `audio_control_error`、`audio_restore_failed` 或异常 stderr。若停机期间确有语音，不应要求系统补执行；恢复后的第一次成功读取会重新建立基线。

保存数据库时优先使用 SQLite 的一致性备份方式或在旧进程彻底退出后处理。不要只拷贝运行中的主 `.sqlite3` 文件而忽略 WAL。

## 6. 按设备逐项执行真实验收

先写明本轮被授权的目标实体、动作及可以恢复的初始状态。只选择白名单内设备。真实测试不默认扩展到全屋操作。

1. 只读记录目标状态。灯需要记录开关及本轮会改变的亮度，窗帘记录可用位置与状态；同时记录相关非目标设备，供事后检查。
2. 先做无动作建议或否定输入，确认没有 `control_home` 执行和 HA 请求。模型生成的静态计划不能代替这一步。
3. 对一个明确设备或已批准的设备组执行一次动作。记录输入来源、session、utterance ID、目标清单、逐项 HA 回执及读回值。
4. 比较实际目标和预期目标，确认未选错房间或漏掉组内设备。检查非目标设备状态。
5. 在允许的环境中复用同一 session、utterance ID 和数据库提交重复事件，确认没有新增 HA 请求或 TTS。不要用重新说同一句代替同 ID 测试。
6. 用新的明确恢复请求恢复本轮改变的状态。只有已知初始状态、恢复动作在授权范围内且不会覆盖别人后续操作时才恢复。无法确定时保留现状并报告。

任意动作返回 `uncertain` 时，先读取当前 HA 和设备状态。不要自动重试、反向操作或换设备。恢复是另一个真实动作，必须有明确目标和可验证结果，不能拿“清理测试”为理由重复发送不确定请求。

音频验收另记是否现场听到，不能用家居 `applied` 代替。测试 31 个虚构动作的摘要只需要自动测试，不需要为了覆盖批量逻辑实际操作 31 台设备。

## 7. 结束时清点进程与限制

确认只留下本次明确要求常驻的一个桥接。所有临时轮询器、零音量探针、测试 watcher 和手工前台实例都已退出。不要在测试结束后留下未来会触发的后台动作。

记录最终服务状态、最终音量、HA 恢复结果和未确认事项。临时探针若改变过音量，退出时必须尝试恢复并读回；恢复失败要明确报告，不能只写 `finally` 已执行。

源项目历史中，一台 S12 完成过零音量 ASR、两轮受控静音播放，以及用户确认只听到一次新回答。原生工具版本另有真实 HA 和合成语音入口验收。首次原生部署的两条窗帘请求曾 `model_failed`，根因未保留，后续正确回放不证明该历史原因已消失。详情见 [故障排查](06-troubleshooting.md)。这些记录不构成公开包、多型号或多日稳定性认证。
