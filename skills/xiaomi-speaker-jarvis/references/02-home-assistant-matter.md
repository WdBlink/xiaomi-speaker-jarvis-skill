# 接入 Home Assistant 与 Matter，并建立可核验的设备目录

本章的完成条件是 HA 可访问且要求认证，Matter 服务可连接，网关与桥接子设备可读取，设备名称及回路对应关系有证据。安装容器成功、配对成功和真实设备动作成功必须分别验收。

公开案例在 Raspberry Pi 4、ARM64 Linux 上运行 Home Assistant Container `2026.10.4` 与 Matter Server `1.5.5`。网关为 Aqara M2，广播型号 `ZHWG19LM`，固件 `4.5.60_0025.0013`。下述数字是这一案例的记录，不是其他家庭的预期数量。

`192.0.2.10` 表示 HA，`192.0.2.20` 表示音箱，`192.0.2.30` 表示 M2。这些是不可直接使用的文档地址。命令中的主机地址、网卡和目录须换成部署现场确认的值。

## 1. 选择安装形态，保留已有服务

空白主机可按 [Home Assistant 的 Matter 文档](https://www.home-assistant.io/integrations/matter/)选择 HA OS 及其 Matter 应用。这是官方支持路径。已有 Linux 与 Docker 的主机可以运行 HA Container，但它没有 Supervisor 和 apps，Matter 需要独立服务。自管 Matter 容器不在 HA 的正式支持范围内。

本案例为复用已有容器主机选择 Container，没有重装系统，没有把 HA OS 镜像写入存储卡。不要把案例中的安装授权扩展成新部署的格式化、恢复出厂或清空数据授权。

先在目标主机运行只读预检。

```sh
uname -m
cat /etc/os-release
df -h
docker version
docker compose version
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
ip -br addr
ss -lnt
```

记录现有服务、容器 ID、启动时间、持久化目录和空闲端口。若 HA 或 Matter 已存在，先检查其配置和数据，不用首次安装命令覆盖。数据目录存在但容器不在时，也不能把目录当作垃圾删除。

成功证据是架构、磁盘、现有容器和网络条件已明确。预检失败时只修复已定位的问题，不默认升级整机、改全局 Docker 代理或关闭 IPv6。

## 2. 固定镜像身份，区分摘要与标签

镜像必须来自上游官方仓库。固定版本和 digest，按目标 CPU 架构选择 manifest。HA 官方容器用法见 [Raspberry Pi Container 安装说明](https://www.home-assistant.io/installation/raspberrypi-other/)，Matter 用法见 [1.5.5 容器说明](https://github.com/matter-js/matterjs-server/blob/v1.5.5/docs/docker.md)。

记录下面四种对象，不能把它们当作相同字段。

| 对象 | 用途 |
|---|---|
| 多架构索引 digest | 锁定版本包含的各平台镜像集合 |
| 目标平台 manifest digest | 锁定该架构的 config 与层 |
| config digest | 校验配置 JSON，包括架构和非 root 用户 |
| `rootfs.diff_ids` | 校验每个解压后文件系统层的 SHA-256 |

联网正常时按确认后的完整引用下载，例如把 `$SOURCE_IMAGE` 设置为官方 `仓库:版本@sha256:实际摘要` 后运行：

```sh
docker pull --platform linux/arm64 "$SOURCE_IMAGE"
docker image inspect "$SOURCE_IMAGE" --format '{{.Architecture}} {{.Config.User}}'
```

不要从其他人的部署文档复制不明 digest，也不要用短摘要前缀替代完整摘要做校验。Docker 的不同镜像存储后端可能展示不同语义的 image ID，应核对命名字段和镜像内容。

## 3. 处理下载失败，避免已导入镜像又被重新拉取

本案例首次失败发生在镜像归档已导入以后，后续 `docker pull` 仍直连 GitHub，因 IPv6 下载超时退出。此时尚未创建服务，不能误报“服务启动失败”。

恢复采用已安装或经过校验的官方 [crane](https://github.com/google/go-containerregistry/releases/tag/v0.22.1) 下载指定 ARM64 镜像归档，或导出已经导入的缓存。下载只使用现场已批准的代理，不修改 Docker 全局代理，也不关闭 TLS 校验。

```sh
crane pull --platform linux/arm64 "$SOURCE_IMAGE" image.tar
```

加载前核对归档中的 `manifest.json`、config SHA-256、`linux/arm64` 架构和每层的解压后 SHA-256。gzip 层必须先解压后与 `rootfs.diff_ids` 比较。只核对文件名、压缩包大小或 `docker load` 返回码不够。

校验通过后再导入，确认导入引用，设置仅本机使用的稳定标签。

```sh
docker load -i image.tar
docker image inspect "$IMPORTED_IMAGE"
docker image tag "$IMPORTED_IMAGE" local/matterjs-server:verified-1.5.5
```

此标签表示已核验的官方镜像缓存，不表示重新构建了软件。后续 Compose 使用 `pull_policy: never`，启动同时指定 `--pull never`。[Docker 的策略定义](https://docs.docker.com/reference/compose-file/services/#pull_policy)明确规定，缓存不存在时直接失败。

遇到损坏层、错误配置摘要或未知架构时停止，不给镜像加“verified”标签。`never` 报镜像不存在时，回到校验和导入步骤；不要为了继续启动临时改为 `latest`。本案例恢复流程已用有效层、gzip 层、错误 config 和错误层分别检查，再在目标主机完成验证。

## 4. 让 HA 先达到可访问、需认证、可恢复

HA 的配置目录必须持久化。按官方 Container 说明建立配置，再验证 Compose 和 HA 配置。下方 `$HA_COMPOSE` 是现场 Compose 文件，`homeassistant` 是本章使用的容器名。

```sh
docker compose -f "$HA_COMPOSE" config --quiet
docker exec homeassistant python -m homeassistant --script check_config --config /config
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8123/
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8123/api/
```

本案例网页返回 `200`，未认证 API 返回 `401`。管理员在 HA 页面自行设置密码，程序与仓库不保存该密码。随后由控制程序使用独立的私有 API token，而不是抓取浏览器账号会话。

通过一次容器重启检查配置和实例身份保留，重启前后核对网页、认证要求及日志。配置 `unless-stopped` 只能说明启动策略，不能替代整机断电恢复测试。案例完成了容器重启验证，没有执行整机断电测试。

HA 网页监听 IPv4 与 Matter 使用 IPv6 是两个层面。本案例将网页限制在 IPv4，同时保留 Matter 所需的局域网 IPv6。不要为了限制网页暴露而关闭整机 IPv6。

## 5. 检查 Matter 的 IPv6 与管理接口边界

Matter 使用 IPv6。M2 与主机已有局域网连接，不需要为了桥接先配置 Thread 网络。选择实际连接家庭网络的接口，例如 `wlan0` 或 `eth0`，不要照抄网卡名。

```sh
printf 'Matter 使用的实际接口名: '
read -r MATTER_INTERFACE
ip -6 addr show dev "$MATTER_INTERFACE"
cat "/proc/sys/net/ipv6/conf/$MATTER_INTERFACE/disable_ipv6"
ss -lnt 'sport = :5580'
```

成功条件包括可用的链路本地 IPv6 地址、`disable_ipv6=0`，以及没有冲突的 5580 监听。`tentative` 地址尚未完成配置，不能视作可用。只有 IPv4 ping 成功不足以证明 Matter 能发现网关。

本案例的配置骨架如下。`./data` 是新的专用持久化目录，已有配对数据必须保留。

```yaml
services:
  matterjs-server:
    image: local/matterjs-server:verified-1.5.5
    pull_policy: never
    container_name: matterjs-server
    network_mode: host
    read_only: true
    restart: unless-stopped
    stop_grace_period: 60s
    environment:
      LISTEN_ADDRESS: "127.0.0.1"
      PRIMARY_INTERFACE: "wlan0" # 替换为已核实的主机接口
      MATTER_LOG_FORMAT: "plain"
    volumes:
      - ./data:/data
```

按已核实镜像的运行用户设置数据目录所有权。本案例镜像使用 `1000:1000`，目录权限为 `0700`，容器使用只读根文件系统，没有启用 privileged 或 Bluetooth。镜像版本改变后，应重新核对用户与存储要求。

`network_mode: host` 用于局域网发现和 Matter 通信；`LISTEN_ADDRESS` 只限制管理 WebSocket。后者默认可监听所有接口，所以必须显式限制。本案例 HA 也使用 host network，因此可以连接 `ws://127.0.0.1:5580/ws`。若 HA 在另一个主机或独立网络命名空间，回环地址不会指向 Matter 容器，不能直接套用此配置。

```sh
docker compose -f "$MATTER_COMPOSE" config --quiet
docker compose -f "$MATTER_COMPOSE" up -d --pull never
docker inspect --format '{{.State.Health.Status}}' matterjs-server
ss -lnt 'sport = :5580'
```

验收要求容器 `healthy`，管理端口只监听 `127.0.0.1:5580`。不创建公网转发。发现失败先检查 host network、网卡、IPv6 和 mDNS，不把管理接口开放到整个网络当作默认修复。

## 6. 先读取服务，再配对网关

在 HA 的“设置 → 设备与服务”添加 Matter，选择已有服务并输入上述 WebSocket 地址。读取 `server_info`，检查服务版本与 schema 是否兼容，再发送只读请求：

```json
{"message_id":"read-only-check","command":"get_nodes","args":{"only_available":false}}
```

这是 [Matter Server 的 WebSocket 请求格式](https://github.com/matter-js/matterjs-server/blob/v1.5.5/docs/websockets_api.md)，须通过已连接的 WebSocket 客户端发送，不是 shell 命令。首次空白服务可返回零节点；已有服务的节点数不能被强行改成零。保存 fabric 身份的哈希用于重启对比，不公开原始标识或数据目录。

确认服务健康、HA 连接成功且授权范围覆盖网关后，在 Aqara App 的网关设置中生成 Matter 配对码。配对会增加控制器访问权限，不能当作纯读取。保留原有 HomeKit 配对，不重置 M2，不从 Apple 家庭移除网关。

本案例 M2 已经联网，使用 `commission_with_code` 和 `network_only: true` 完成配对。请求形态如下，`REPLACE_WITH_PRIVATE_CODE` 必须来自当次有效配对窗口：

```json
{"message_id":"pair-m2","command":"commission_with_code","args":{"code":"REPLACE_WITH_PRIVATE_CODE","network_only":true}}
```

优先在 HA 或本地管理界面输入配对码。不要将真实配对码写入命令参数、Git、公开日志或聊天。接口参数见[上游 commissioning 文档](https://github.com/matter-js/matterjs-server/blob/v1.5.5/docs/websockets_api.md#commissioning)。超时先读取节点清单，确认是否已经配对，不能直接重置设备重试。

成功证据是网关身份符合预期、节点可用、HA 导入设备，并且桥接子设备可读。本案例配对后 HomeKit mDNS 仍显示 `sf=0`，支持“原配对仍存在”的判断；未从 Apple 家庭实测动作，不能据此声称两个平台的每项控制都验收通过。

## 7. 比较子设备、HA 设备与实体数量

本案例 Aqara 显示 19 个子设备，但 HA 导入的是 M2 加 16 个桥接子设备，共 17 个设备、155 个实体。桥接子设备包括 12 个灯控面板和 4 个窗帘设备，全部报告可达。

155 个实体包含事件和诊断信息，不代表 155 个独立负载。19 个 Aqara 子设备也不保证全部由当前固件导出为 Matter。未出现的设备应标为未导出或未核实，不按型号猜测它已接入。

建立私有清单，逐项记录网关子设备、HA 设备、序列号、实体及能力。对每个控制实体读取有效状态，排除 `unknown`、`unavailable` 和只读诊断实体。墙壁开关即使暴露为 `light`，也不自动具有调光能力。

## 8. 用稳定标识匹配设备，再逐回路命名

本案例 Aqara 自定义名称与房间没有经 Matter 自动导入。处理时先将 Aqara 设备 ID 与 HA 序列号精确匹配，再确认面板的每一路回路，不能按清单顺序或同型号外观猜测。

建议保留以下映射结构。所有真实标识留在 Git 外，仅在发布示例中使用占位值。

| Aqara 设备 ID | HA 序列号 | HA 实体 | 物理用途 | HA 区域 |
|---|---|---|---|---|
| 私有设备标识 A | 与 A 一致的序列号 | `light.living_room_main` | 客厅主灯 | 客厅 |
| 私有设备标识 A | 与 A 一致的序列号 | `light.bedroom_accent` | 卧室氛围灯 | 卧室 |

同一面板可以控制另一个房间的负载。设备安装区域和每个实体的实际区域可以不同，独立回路应分配到实际服务的房间。用途不明的回路保留“左键”等中性名称，不由模型编造用途。

先使用序列号和 App 状态完成只读映射。仍有歧义且已获动作测试授权时，每次只测试一路，动作前读取新鲜状态，动作后核对 HA 与现场或厂商 App，再恢复原状态。不要通过一次全屋切换来识别回路。

命名优先改显示名与区域，保留既有实体 ID，减少破坏自动化引用的风险。需要程序批量操作时，使用部署版本的[设备注册表](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/config/device_registry.py)和[实体注册表](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/config/entity_registry.py)接口，不直接编辑 HA 的存储文件。

## 9. 交付控制目录，明确真实验收边界

将确认过的控制实体、房间、能力和别名写入 Jarvis 的私有白名单，HA token 独立保存。准确字段、文件权限与入口命令由[参考运行时说明](07-reference-runtime.md)维护，不把整个 HA 实体清单作为默认授权范围。

公开案例随后验收了 27 路开关与 4 台窗帘。每项先读取原状态，再做一次状态变化或短行程移动，最后恢复，共 62 次动作与恢复获得 HA 状态确认。所有最终状态回读与恢复目标一致。

这些结果是设备状态反馈，部分有 Aqara App 旁证，没有独立物理测量。窗帘只验证短行程位置变化，未验证全程开合、运动中停止或障碍保护。安装阶段和状态读取阶段本身均不包含这些动作验收。

最后检查 Matter 容器重启后 fabric 身份不变，HA 仍可访问，既有容器 ID 与启动时间未被意外改变。保留 HA 配置、Matter 数据和必要脱敏记录，清理临时镜像归档、安装目录与配对码文件。未测试的整机断电恢复、长期稳定性和其他设备类型，应继续标为未验证。
