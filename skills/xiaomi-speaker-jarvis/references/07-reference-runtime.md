# 安装和运行参考实现

运行时随 Skill 放在 `assets/runtime`，不依赖原作者的私有仓库或家庭配置。以下命令从克隆后的仓库根目录开始；所有示例地址、实体和凭据都必须替换。它是已提炼的参考实现，不是可直接扫描并接管所有型号的一键安装器。

## 1. 建立隔离环境

基线为 Python 3.12，支持 macOS 和 Linux。生产场景的 launchd 模板只用于 macOS。不要复用别的项目环境后误把其已安装模块当成本仓库代码。

```bash
cd skills/xiaomi-speaker-jarvis/assets/runtime
JARVIS_RUNTIME="$(pwd)"
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m home_jarvis.xiaomi_voice --help
```

依赖是 `python-miio==0.5.12`、`requests==2.34.2`。扫码脚本直接下载官方 PNG，不需要二维码生成库。HA、模型、SQLite、云记录读取和服务循环主要使用标准库。

确认实际导入位置：

```bash
.venv/bin/python - <<'PY'
import home_jarvis.core
from pathlib import Path
actual = Path(home_jarvis.core.__file__).resolve()
expected = (Path.cwd() / "src/home_jarvis/core.py").resolve()
assert actual == expected, (actual, expected)
print("Using the bundled runtime")
PY
```

`python -m home_jarvis` 不带文本只运行隔离 Mock demo。它通过不代表网络、账号、HA、音箱或模型可用。带文本的这个 CLI 仍是无模型有限文本入口；真实语音 Agent 使用 `home_jarvis.xiaomi_voice`。

## 2. 私有配置放到 Git 之外

目录分两部分：

```text
仓库内：assets/runtime/src、tests、config-examples
仓库外：~/.config/home-jarvis/
  home.json
  ha-token
  speaker.json
  minimax.json
  minimax-api-key
  xiaomi-cloud-<真实DID>.json
  xiaomi-voice.sqlite3
  xiaomi-voice.stdout.log / xiaomi-voice.stderr.log
```

`<真实DID>` 是说明文字，不是可粘贴的文件名。不要用符号链接规避凭据校验，不要把整个 `.config` 放入仓库。

下面的初始化只创建尚不存在的配置，不覆盖已有文件；会修正模板里的凭据文件路径，但保留无效的设备占位值待你填写。

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import json, os
private = Path.home() / ".config/home-jarvis"
private.mkdir(parents=True, exist_ok=True, mode=0o700)
private.chmod(0o700)
for name in ("home", "speaker", "minimax"):
    target = private / f"{name}.json"
    if target.exists() or target.is_symlink():
        print(f"Preserved existing {name}.json")
        continue
    data = json.loads((Path("config-examples") / f"{name}.example.json").read_text())
    if name == "home":
        data["token_file"] = str(private / "ha-token")
    if name == "minimax":
        data["api_key_file"] = str(private / "minimax-api-key")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Created {name}.json with nonfunctional examples")
PY
```

用本机编辑器填写私有 JSON。值的含义如下：

| 文件 | 精确字段 | 注意 |
| --- | --- | --- |
| `home.json` | `schema_version, base_url, token_file, owners, default_room, rooms, devices` | 先按 HA 实体核对目录。语音桥当前 owner 固定为 `resident`，必须在允许列表中 |
| `speaker.json` | `localip, did, mac, model, token` | `did` 是正 32 位十进制 miIO ID；token 为 32 位十六进制；model 当前必须为 `xiaomi.wifispeaker.s12` |
| `minimax.json` | `endpoint, model, api_key_file` | 当前仅允许官方 HTTPS `/v1/chat/completions`；key 文件绝对路径 |
| 云登录文件 | `userId, serviceToken, deviceId, miotDID, hardware` | 由登录脚本生成，不拿 miIO token 填 `serviceToken`；`deviceId` 与 `miotDID` 不相同 |

`cloud.example.json` 只说明格式，不应复制为有效云会话。speaker 的 token 由[发现与账号章节](01-discovery-and-auth.md)的授权账号流程取得；不要为了凑格式生成随机 token。

HA 长效令牌由已登录的 HA 用户在其账户安全设置中创建。模型 key 使用自己的提供商账号。可在本机终端隐藏输入这两种值，避免进入 shell 历史；若文件已有值先保留，不自动覆盖：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from getpass import getpass
import os
private = Path.home() / ".config/home-jarvis"
for name, label in (("ha-token", "HA access token"), ("minimax-api-key", "MiniMax API key")):
    path = private / name
    if path.exists() or path.is_symlink():
        print(f"Preserved existing {name}")
        continue
    value = getpass(label + " (hidden): ").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Expected a single nonempty secret")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value + "\n")
    print(f"Saved {name}; value not displayed")
PY
```

用现有凭据时检查文件所有者、普通文件属性和 `0600` 权限。密码、验证码和二维码确认不要交给聊天；工具宿主若拒绝终端控制，不换另一种桌面自动化绕过，而是让用户完成必要的本机输入。

## 3. 单独取得小米云识别会话

先确认声卡设备对应的真实 DID。以下变量只存设备 ID，不存密码或 token：

```bash
printf 'Speaker miIO DID: '
read -r JARVIS_DID
.venv/bin/python scripts/xiaomi-speaker/login.py --did "$JARVIS_DID"
```

程序输出 `scan_required` 后，在另一个终端打开它报告的 PNG，或用当前 Agent 宿主的图片工具直接显示该文件。macOS 的固定默认位置是：

```bash
open "$HOME/.config/home-jarvis/xiaomi-login-qr.png"
```

第一进程必须继续轮询。不要只显示二维码文件名就要求用户扫码。用户用绑定音箱的小米账号完成手机确认后，等待程序输出 `authenticated`，再验证云会话。登录结束会删除二维码；手机显示成功但未保存本地会话时，不能跳过 token 交换。

脚本生成 `~/.config/home-jarvis/xiaomi-cloud-<DID>.json`。它只保留目标音箱所需字段，不保存账号密码或 passToken。详细 STS 401 恢复路径见认证章节。

## 4. 先做只读链路检查

下面的检查读取私有文件，但不会打印凭据、请求 TTS 或控制设备。网络动作只有 HA 查询和小米设备身份查询：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from home_jarvis.home import configured_gateway
from home_jarvis.speaker import XiaomiSpeaker
from home_jarvis.xiaomi_cloud import XiaomiCloud
import time
private = Path.home() / ".config/home-jarvis"
speaker = XiaomiSpeaker(private / "speaker.json")
cloud = XiaomiCloud(private / f"xiaomi-cloud-{speaker.miot_did}.json")
cloud.verify_device()
assert cloud.miot_did == speaker.miot_did
gateway = configured_gateway(private / "home.json")
snapshot = gateway.query_home(gateway.default_room, time.time())
assert snapshot.observation_error is None, snapshot.observation_error
print({"cloud_identity_verified": True, "devices": len(snapshot.devices),
       "timezone": snapshot.timezone, "local_time": snapshot.local_time})
PY
```

这一步只校验 speaker 配置格式；局域网 `miIO.info` 还要按发现章节用真实 token 验证。云读取器会拒绝同一账号存在多个相同 hardware 而无法唯一绑定的情况，不要删掉这道检查然后任选最近一条转写。

先进行一个**只规划、不执行**的模型探针：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from home_jarvis.home import configured_gateway
from home_jarvis.planner import MiniMaxPlanner
import time
private = Path.home() / ".config/home-jarvis"
gateway = configured_gateway(private / "home.json")
planner = MiniMaxPlanner(private / "minimax.json", gateway)
plan = planner.plan("根据现在的情况，先建议应该关闭哪些窗帘，不要实际操作。",
                    query_home=lambda: gateway.query_home(gateway.default_room, time.time()))
print({"reply": plan.message, "proposed_action_count": len(plan.actions),
       "model_calls": plan.model_calls, "executed": False})
assert not plan.actions, "Advice probe unexpectedly proposed control; inspect before enabling execution"
PY
```

模型会收到设备目录、原句和脱敏状态，且可能产生 API 费用。这个命令没有 Core 执行器，不会把模型返回的动作发给设备。成功只能证明规划接口；实际执行另测。

## 5. 验证真实模型动作与回执

在用户已授权控制和测试目标的范围内选一条可观察动作。先记录状态；不要测试门锁、警报或这份实现没有授权的设备。下面是代码结构示例，输入应换成已核对目录内的目标：

```python
from pathlib import Path
from uuid import uuid4
from home_jarvis.core import Core, Utterance
from home_jarvis.home import configured_gateway
from home_jarvis.planner import MiniMaxPlanner

private = Path.home() / ".config/home-jarvis"
gateway = configured_gateway(private / "home.json")
planner = MiniMaxPlanner(private / "minimax.json", gateway)
core = Core(private / "acceptance.sqlite3", gateway, planner=planner)
try:
    core.open_session("acceptance", room_id=gateway.default_room)
    event = Utterance("acceptance", str(uuid4()), "resident", "qwen3-asr", "把客厅的灯关掉")
    result = core.submit(event)  # Real control may occur here.
    duplicate = core.submit(event)
    assert duplicate == result
    print(result)
finally:
    core.close()
```

`qwen3-asr` 在这段文本验收里只是会话来源标识，不代表运行了 Qwen ASR。真实音箱桥固定为 `xiaomi-cloud`。同输入 ID 不重放的验收还应检查 outbox/服务请求计数，不能仅凭回执字符串相等。

不要把这一段悄悄放进安装器或只读探针。恢复原状态是另一个已授权动作，需要新输入 ID；结果不确定时先回读，不自动反向写入。

## 6. 前台启动真实语音桥

只允许一个进程持有同一个语音 DB 的锁；启动前停止旧实例，避免不同数据库的两个桥同时控制同一音箱。

```bash
.venv/bin/python -m home_jarvis.xiaomi_voice \
  --cloud-config "$HOME/.config/home-jarvis/xiaomi-cloud-$JARVIS_DID.json" \
  --speaker-config "$HOME/.config/home-jarvis/speaker.json" \
  --home-config "$HOME/.config/home-jarvis/home.json" \
  --model-config "$HOME/.config/home-jarvis/minimax.json" \
  --quiet-native-replies --reply-volume 16 \
  --db "$HOME/.config/home-jarvis/xiaomi-voice.sqlite3"
```

确认 `ready` 显示 `input_mode: all_transcripts`、`command_prefix: null`、配置模型、`audio_mode: quiet_native_replies`。服务先建立云记录基线，启动前的对话不会补执行。

现场先说“小爱同学”，再说具体命令，随后测房间类别、否定、建议与停止。检查云转写与真实结果，不要把小米自己完成的动作误算成 Jarvis 成功。确认播报起止、待机归零、第二轮仍能唤醒；详见[分层验收](05-operations-and-validation.md)。

`Ctrl-C` 或正常 SIGTERM 会尝试恢复播报音量。强杀、掉电、账号失效和网络中断不能保证音量恢复。不要先把静音服务强杀，再让用户用听不到的音箱排障。

## 7. 设置常驻

模板是 [launchd.example.plist](../assets/runtime/config-examples/launchd.example.plist)。先替换所有 `/EXAMPLE/` 绝对路径，确保日志父目录存在，再用 `plutil -lint` 校验。不要在 plist 中写密钥或 token。

launchd 不展开 `~`、`$HOME`、shell 命令替换和虚拟环境激活。`ProgramArguments[0]` 指向实际 `.venv/bin/python`，`WorkingDirectory` 指向安装的 runtime；所有配置、DB 与日志都是绝对路径。`KeepAlive` 负责退出后拉起，不替代状态恢复或健康检查。

用[运维章节](05-operations-and-validation.md)的流程注册和更新：先证明无动作正在发送，`bootout` 后等待旧 PID 真正退出，再 `bootstrap`。重新注册失败时保留错误并恢复服务，不能只报告“代码已更新”。

## 8. 改成其他模型时保持同样边界

当前 `planner.py` 明确允许官方 MiniMax endpoint 和模型列表，防止误把凭据发往任意 URL。换提供商是代码适配，不是把配置中的 endpoint 指到任意代理。

替换 planner 时保持 `plan(text, *, query_home=...) -> ModelPlan` 契约、完整工具回合、实体允许列表、不把凭据送给模型，以及 Core/outbox/取消语义。先用模拟工具回合测试，再做不写设备的真实模型探针，最后才进真实动作。不要退回到“模型 JSON + 原句正则批准”的旧路线。
