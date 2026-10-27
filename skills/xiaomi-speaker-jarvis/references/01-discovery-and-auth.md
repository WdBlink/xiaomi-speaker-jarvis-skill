# 发现音箱并完成两套认证

本章的完成条件是确认目标音箱的身份，分别取得可用的 miIO 设备凭证和 `micoapi` 账号会话。发现设备、扫码确认、保存文件和认证成功是四个不同结果，不能互相替代。

公开案例的实测基线是标签 `MDZ-25-DA`、本地型号 `xiaomi.wifispeaker.s12`、固件 `1.76.54`。这些结果只适用于被测试的设备。未知型号先做只读发现，不直接套用后续 S12 控制命令。

示例中 `192.0.2.10` 表示 HA 主机，`192.0.2.20` 表示音箱，`192.0.2.30` 表示网关。它们属于 [RFC 5737 文档地址](https://www.rfc-editor.org/rfc/rfc5737)，不可直接用于真实部署。下文 `$SPEAKER_IP`、`$SPEAKER_DID` 均须由你现场核对后输入。

## 1. 先确定发现范围和证据格式

目的在于把标签、路由器记录、局域网协议与云端清单连到同一台设备。只查看你有权管理的设备和网络，先从路由器的已连接终端列表寻找候选 IP。

在私有笔记记录下列字段，不把完整清单提交到 Git。

| 字段 | 来源 | 能证明什么 |
|---|---|---|
| 产品型号 | 机身标签 | 产品身份线索，不是当前运行固件 |
| IP、MAC、终端名称 | 路由器、ARP 邻居表 | 当前网络位置，名称可能被修改 |
| miIO DID | UDP 握手 | miIO 协议身份，不能代替账号授权 |
| `model`、`fw_ver`、`mac` | 认证后的 `miIO.info` | 当前设备上报的型号、固件与网卡身份 |
| 云端 DID、MAC、model | 自有账号设备清单 | 账号中与局域网候选设备的对应关系 |

成功证据是多个独立来源一致。只有名称相似，或只有一个开放端口，仍不足以锁定设备。DHCP 可能改变 IP，应优先保留 DID 与 MAC 的对应关系。

## 2. 用 ARP、mDNS 和有界端口检查缩小范围

在 macOS 的私有终端输入实际 IP。不要把示例地址当作设备地址。

```sh
printf '音箱的实际 IPv4 地址: '
read -r SPEAKER_IP
export SPEAKER_IP
arp -an
dns-sd -B _miio._udp local.
```

`dns-sd` 会持续监听，观察一段时间后按 `Ctrl-C` 结束。没有广播不代表没有 miIO。Linux 可用 `ip neigh` 查看邻居表；已安装 Avahi 时，可用 `avahi-browse -rt _miio._udp` 查询广播。

针对已经确认的单个候选 IP 检查少量端口。例如在 macOS 上运行：

```sh
nc -vz -G 2 "$SPEAKER_IP" 22 80 443
```

端口开放只说明 TCP 服务接受连接。端口关闭不排除 UDP 服务。扫描没有发现 SSH，也不妨碍下一步的 miIO 握手。

本章给出可复用的发现步骤，未把全部 ARP、mDNS 和端口命令标为本案例已逐项验收。实践中早期局域网广播没有公开可用的型号与固件信息；得到 token 后才由 `miIO.info` 确认。可靠身份结论来自标签、路由器、云端清单、UDP 回包和认证后的 `miIO.info`。

## 3. 发送 UDP 54321 握手，不把握手当作认证

按[参考运行时说明](07-reference-runtime.md)安装 `python-miio` 后，在运行时目录执行：

```sh
.venv/bin/miiocli discover --help
.venv/bin/miiocli discover
```

发现命令可能枚举同网段其他设备，也可能输出凭证材料，只在不录屏、不共享日志的私有终端运行。只保留目标设备的脱敏结论。[python-miio 的发现说明](https://python-miio.readthedocs.io/en/latest/discovery.html)区分了握手发现和 mDNS 发现，两者提供的信息不同。

若要把请求限定到候选 IP，可用以下单次握手。它只打印 DID，不打印响应尾部可能包含的 token。

```sh
.venv/bin/python - <<'PY'
import ipaddress, os, socket, struct
ip = ipaddress.IPv4Address(os.environ["SPEAKER_IP"])
assert ip.is_private and ip not in ipaddress.ip_network("192.0.2.0/24")
assert not ip.is_unspecified and not ip.is_multicast and not ip.is_loopback
hello = bytes.fromhex("21310020" + "ff" * 28)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.settimeout(3)
    sock.sendto(hello, (str(ip), 54321))
    reply, sender = sock.recvfrom(2048)
assert sender == (str(ip), 54321), "Unexpected response source"
assert len(reply) == 32 and reply[:4] == bytes.fromhex("21310020"), "Unexpected handshake"
did = struct.unpack(">I", reply[8:12])[0]
assert 0 < did < 0xffffffff, "Invalid device ID"
print({"miio_handshake": "received", "did": did})
PY
```

包格式与端口可核对 [python-miio 0.5.12 协议实现](https://github.com/rytilahti/python-miio/blob/0.5.12/miio/protocol.py)。此片段是只读复用探针，不是新一轮真实设备验收。

成功证据是目标地址返回结构正确的 miIO 握手及合理 DID。UDP 超时只能说明此时未收到响应，先检查同网段、访客网络隔离、IP 变化和防火墙，不连续发送猜测命令。响应尾部全 `0` 或全 `f` 不能当作有效 token。

## 4. 通过设备所有者的账号取得 miIO token

miIO token 用于加密和认证本地命令。它不是管理员密码，也不是 SSH 密钥。没有合法账号会话、设备所有者提供的凭证或已有授权备份时，停在这里，不能从型号、序列号或 DID 猜 token。

本案例经设备所有者扫码授权，复用了 [Xiaomi-cloud-tokens-extractor 的固定版本](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor/blob/c4db715dace9806e905153c2977608873e8ab7c9/token_extractor.py)。获取阶段按事先确认的 DID 和 MAC 精确筛选目标，只保存该音箱的凭证。公开运行时没有把当时的一次性提取探针包装成通用安装器。

复用时遵循以下顺序。

1. 阅读固定版本的[上游说明](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor/blob/c4db715dace9806e905153c2977608873e8ab7c9/README.md)及许可证，确认登录服务和代码来源。
2. 由设备所有者在官方小米认证页面完成登录，不把账号密码、验证码或二维码交给第三方网站。
3. 在私有进程内读取账号设备清单。用 DID 和 MAC 交叉匹配，型号也必须一致；冲突、零匹配或多匹配都停止。
4. 仅将选中音箱的字段写到 Git 工作树外的私有配置。字段名和验证规则以[参考运行时](07-reference-runtime.md)为准。
5. 用一次 `miIO.info` 验证 token，再开始控制实验。

上游通用提取工具可能显示账号下所有设备和 token。不要把这类输出交给聊天记录、CI 日志或公开 issue。目录设为 `0700`，凭证文件设为 `0600`，拒绝符号链接及不属于当前用户的文件。

```sh
umask 077
mkdir -p "$HOME/.config/home-jarvis"
chmod 700 "$HOME/.config/home-jarvis"
```

权限设置只是存储条件。它不能证明 token 有效，也不能修复设备匹配错误。

### 首次提取的具体操作

下面是调用固定上游版本的复用方法，不是本次公开包重新完成的一次真实账号登录。先在自己的私有终端创建临时目录；克隆和安装失败时停止，不继续执行后面的命令。

```sh
umask 077
JARVIS_EXTRACTOR="$(mktemp -d "${TMPDIR:-/tmp}/jarvis-token.XXXXXX")"
git clone https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor.git "$JARVIS_EXTRACTOR/source"
cd "$JARVIS_EXTRACTOR/source"
git checkout --detach c4db715dace9806e905153c2977608873e8ab7c9
git rev-parse HEAD
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

先阅读该目录的许可证、`requirements.txt` 和登录实现。该提取器需要 Pillow 等额外依赖，与本仓库仅用于 `micoapi` 的 `login.py` 不同。它的图片服务器默认监听所有网络接口；`--host` 只改展示地址，并不限制监听。登录前对这份临时副本做一处可核对的修改，让二维码只在本机可见：

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
path = Path("token_extractor.py")
source = path.read_text()
old = 'HTTPServer(("", 31415), ImgHttpHandler)'
assert source.count(old) == 1, "Upstream changed; inspect before login"
path.write_text(source.replace(old, 'HTTPServer(("127.0.0.1", 31415), ImgHttpHandler)'))
PY
git diff -- token_extractor.py
.venv/bin/python token_extractor.py --server cn --log_level CRITICAL
```

`cn` 必须换成设备实际绑定地区；不确定时先查看米家地区设置。按以下顺序完成：

1. 在 `p/q` 提示选择 `q`。打开程序给出的本机二维码页面（通常为 `http://127.0.0.1:31415`），由绑定设备的小米账号扫码。若提示保存了图片，则打开该图片，保持原进程等待回调。
2. 登录后只查看已核对 DID 对应的一项，逐项比较 `ID`、`MAC`、`MODEL`；其中 `ID` 应等于 UDP 握手 DID。任一不符时停止，不按名称相似度选设备。
3. 将这一项的 `TOKEN` 保留在本机，供下一节隐藏输入验证。不要使用 `--password` 参数或 `--output` 全量导出，也不要将终端完整输出粘贴到聊天。`CRITICAL` 关闭调试细节，但不会隐藏设备清单中的 token。
4. 验证通过后，在[运行时初始化](07-reference-runtime.md)生成的私有 `speaker.json` 填入 `localip`、十进制字符串 `did`、`mac`、`model` 和 `token`。本机编辑，保持 `0600` 权限；不要把 `fw_ver` 当作 `model`。
5. 按回车结束提取器，确认图片服务退出，删除它报告的临时二维码图片及本次专用提取目录。先离开该目录；仅删除刚创建且已核对的路径，不使用宽泛通配符清理。

后续日常启动不需要每次重新提取。若 token 认证失败，先核对设备是否重置、IP 是否改变及地区是否正确；重新获取也只覆盖已经确认失效的目标凭证。云转写仍须单独执行第 6 节，不能把这次 `xiaomiio` 登录结果直接作为 `micoapi` 会话。

## 5. 用 miIO.info 确认当前型号与固件

回到 `skills/xiaomi-speaker-jarvis/assets/runtime`，使用第 7 章建立的运行时虚拟环境，不要留在临时提取目录。下面的只读检查通过终端隐藏输入接收 token，不把 token 放进命令参数。先把握手 DID 与云端记录对齐，再运行。不要启用协议调试日志。

```sh
.venv/bin/python - <<'PY'
import getpass, os, re
from miio import Device
token = getpass.getpass("目标音箱的 miIO token: ")
assert re.fullmatch(r"[0-9a-fA-F]{32}", token), "Invalid token format"
device = Device(ip=os.environ["SPEAKER_IP"], token=token,
                timeout=3, lazy_discover=False)
info = device.send("miIO.info", [], retry_count=0)
assert isinstance(info, dict), "Unexpected device info"
print({key: info.get(key) for key in ("model", "fw_ver", "hw_ver")})
PY
```

公开案例读到 `model=xiaomi.wifispeaker.s12`、`fw_ver=1.76.54`、`hw_ver=Linux`。`Linux` 不是 CPU 架构或音频 ABI 的证据。路由器管理页面的版本也不能当作音箱固件。

这个回包证明该 token 可完成这台设备的 miIO 认证。它不证明 TTS 已发声、云转写可读、SSH 可登录或原始 PCM 已取得。失败时核对 IP、DID、MAC、账号地区和 token 是否变化，不通过自动重试掩盖身份冲突。

## 6. 单独登录 micoapi，并让用户真正看到二维码

云转写需要第二套凭证。两套身份不可互换。

| 名称 | 用途 | 不能替代什么 |
|---|---|---|
| miIO `token` | 音箱本地 UDP 命令 | 云端账号会话 |
| `userId`、`serviceToken` | `micoapi` 账号会话 cookie | miIO token |
| 云清单 `deviceID`，保存为 `deviceId` | MiNA 云端音箱标识 | 数字形式的 miIO DID |
| `miotDID` | 将云端音箱对应到已核实的 miIO DID | 声纹、用户身份或房间授权 |
| `passToken`、`ssecurity`、nonce | 登录交换中的临时材料 | 应长期落盘的配置字段 |

在[参考登录脚本](../assets/runtime/scripts/xiaomi-speaker/login.py)所在运行时目录执行。这里输入已核实的 DID，不使用文档中的虚构数字。

```sh
printf '已核实的音箱 miIO DID: '
read -r SPEAKER_DID
export SPEAKER_DID
.venv/bin/python scripts/xiaomi-speaker/login.py --did "$SPEAKER_DID"
```

脚本输出 `scan_required` 后，从另一个本地终端打开它报告的 `qr_file`。macOS 默认路径的命令如下：

```sh
open "$HOME/.config/home-jarvis/xiaomi-login-qr.png"
```

实践中曾连续两次出现用户没有看到二维码，后来又出现手机已确认但本地会话未保存。Agent 必须确认二维码已在用户可见的界面打开或渲染，不能把文件路径出现当作“用户已看到”。用户用小米官方 App 扫码并确认。二维码和登录链接都是临时认证材料，不上传到 Git、不发送到公开图片站，也不长期保留截图。

当前脚本按服务端 `timeout` 等待，并把本地等待时长限制在 30 至 300 秒。过期后生成新二维码，不能重复展示已失效的旧图片。结束或失败时会清理临时 PNG；若进程被强制杀死，应确认残留文件已删除。

成功必须同时满足 `status=authenticated`、目标 `miotDID` 唯一匹配、硬件属于 `S12` 或 `S12A`，并且服务会话能够读取设备列表。手机显示“扫码成功”只覆盖其中一段。

## 7. 按失败阶段恢复，不让用户反复扫码碰运气

认证失败时，先保存脱敏的 `stage`、`exception_class` 和状态码，定位失败发生在扫码前、扫码后还是保存会话时。手机确认后应沿用同一登录会话处理回调，不能丢弃会话再把缺少 cookie 的结果当作设备故障。需要重新扫码时，先解释前一次本地交换未完成。

| 失败阶段 | 处理与停止点 |
|---|---|
| `setup`、`qr_request`、`qr_image` | 检查官方 HTTPS 服务可达性和响应格式，不能跳过域名校验 |
| `qr_poll` 超时 | 清理旧图，重新生成一张可见二维码；不要沿用过期会话 |
| 用户已确认但 `sts` 返回 401 | 允许脚本执行下述一次受限回退 |
| `device_match` 失败 | 核对账号、地区、DID 和设备硬件；不改成选列表第一台 |
| `save_session` 失败 | 检查路径是否在 Git 内、目录权限和磁盘；不输出 token 辅助排错 |

本案例的已验证恢复路径是直接 STS 请求返回 `401` 后，用 QR 结果中的 `passToken` 在干净新会话内请求 `sid=micoapi`。脚本验证账号和回调一致，用新 nonce 与 `ssecurity` 生成 `clientSign`，追加到该次交换返回的原始 STS URL。

安全边界写在脚本中。只接受 HTTPS 和明确的小米官方主机，STS 必须是 `api2.mina.mi.com/sts`。禁止跨域跟随重定向、禁止已有签名再签一次，其他状态不触发这一回退，回退也不无限循环。`trust_env=False` 防止从进程环境自动继承代理设置。

只有得到服务 token 且精确匹配设备后才原子替换会话文件。失败时保留旧配置；密码与 `passToken` 不落盘。当前云接口没有本项目可依赖的长期稳定性承诺，凭证过期或接口改变应重新验证，不能声称自动永久续期。

## 8. 在 shell 与原始音频的边界停止

特定 S12A、固件 `1.76.54` 的现场记录中，用户操作 CH 与麦克风静音键后，TCP 22 开放并出现 Dropbear、`Mico S12A` 横幅，但认证被拒绝，始终未取得 shell。这个按键事实不适用于其他型号，也不是本教程要求执行的步骤。

静态固件核验显示原厂登录存在签名认证边界，开放端口不提供所需私钥。miIO token、SN、设备 DID 和普通推导密码都不能替代有效认证。当前 Skill 不提供 root、降级、分区写入或恢复模式操作配方。

上游 [S12 首次接入说明](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/e2b6fb729c0c95c53a867959f15e1a783f1f020f/docs/getting-started/bringup.md)涉及额外硬件和系统维护条件。本案例没有验证这些步骤，也没有采集原始 PCM。需要原始音频的任务应在这里报告阻断，不能把云端转写称为本地麦克风接管。

完成本章后，保留脱敏的型号、固件、身份一致性、认证阶段和凭证权限结论。继续 [Home Assistant 与 Matter 接入](02-home-assistant-matter.md)，或回到[参考运行时](07-reference-runtime.md)配置音箱与云转写。
