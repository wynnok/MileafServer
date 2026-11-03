# 部署脚本说明

本目录提供新站点的一键部署脚本：基于 Docker 部署 TeslaMate、PostgreSQL 和
Mileaf Server，按需安装 MQTT（Mosquitto）和 Grafana。
已有 TeslaMate、只需接入 Mileaf 的站点，请使用[自托管安装指南](../docs/installation.md)。

## 快速开始

将完整仓库放到目标机器，在**仓库根目录**执行：

```bash
bash deploy/install.sh
```

脚本会询问是否安装 MQTT 和 Grafana，首次默认都不安装。如果当前就在 `deploy` 目录，
执行 `bash install.sh` 即可。脚本依赖本仓库的 Dockerfile、源码及本目录配置，不能只下载
`install.sh` 单独运行，也不适合通过 `curl | bash` 执行。

无人值守安装可以显式选择组件：

```bash
# 仅安装 TeslaMate、PostgreSQL、Mileaf Server
bash deploy/install.sh --non-interactive --no-mqtt --no-grafana

# 安装全部五个组件
bash deploy/install.sh --non-interactive --mqtt --grafana

# 只增加 Grafana
bash deploy/install.sh --non-interactive --no-mqtt --grafana
```

## 环境要求

| 环境 | 脚本行为 |
|---|---|
| Ubuntu / Debian，使用 systemd，amd64 / arm64 | 缺少 Docker 时，通过官方 apt 仓库安装 Engine、Buildx、Compose，并启动 Docker |
| 已有可用 Docker 的 Linux / macOS | 复用现有 Docker，检查 Compose 与主机架构 |
| macOS 缺少 Docker | 提示先安装并启动 Docker Desktop，再重跑脚本 |
| 其他发行版或已有软件包冲突 | 停止并提示处理，不自动卸载现有容器运行时 |

需要 Compose v2.20+、amd64 或 arm64 Docker 主机，以及可访问镜像仓库、构建依赖源和
Tesla 服务的网络。已有 Docker 但缺少 Compose 插件时，需要先补装插件。
宿主机无需安装 Python 或 uv，Mileaf 的构建在 Docker 内完成。

安装或启动 Docker 需要 root / sudo；`--non-interactive` 只关闭组件选择询问，
不会绕过 sudo 验证。无人值守运行需以 root 执行或具备免密码 sudo 权限。

## 参数

| 参数 | 说明 | 首次默认值 |
|---|---|---|
| `--mqtt` / `--no-mqtt` | 启用 / 关闭 Mosquitto，并同步配置 TeslaMate MQTT 发布 | 关闭 |
| `--grafana` / `--no-grafana` | 启用 / 关闭 Grafana | 关闭 |
| `--non-interactive` | 不询问组件选择，使用显式参数或保存的选择 | 交互终端中询问 |
| `--bind ADDRESS` | 三个 HTTP 服务共同绑定的 IPv4 地址 | `127.0.0.1` |
| `--teslamate-port PORT` | TeslaMate 宿主机端口 | `4000` |
| `--mileaf-port PORT` | Mileaf 宿主机端口 | `8080` |
| `--grafana-port PORT` | Grafana 宿主机端口 | `3000` |
| `--data-dir DIR` | 部署配置目录，**不是数据库存储目录** | 仓库根目录下 `.deployment` |
| `--prepare-only` | 仅生成或更新配置，不检查 / 安装 Docker，不启动容器 | 关闭 |
| `-h` / `--help` | 显示帮助 | — |

端口范围为 `1–65535`，已启用的 HTTP 服务不能使用相同端口。相对路径形式的
`--data-dir` 相对于执行命令时的工作目录，建议使用绝对路径。
非交互输入（没有终端）也会直接使用指定选项或保存的选择。

```bash
# 使用独立配置目录，并调整 Mileaf 端口；后续重跑保持同一个 --data-dir
bash deploy/install.sh --data-dir /srv/mileaf-config --mileaf-port 8081

# 仅准备配置，稍后执行安装
bash deploy/install.sh --prepare-only --non-interactive --mqtt --grafana
```

## 安装过程与网络

脚本依次保存配置、检查 Docker、拉取所选镜像、构建 Mileaf，然后启动 PostgreSQL。
随后调用 TeslaMate 官方入口完成数据库迁移，配置只读账号，在发布 Mileaf HTTP 服务前
初始化访问口令，最后启动所选服务并检查就绪状态及 TeslaMate HTTP 可达性。

| 组件 | 镜像 / 构建来源 | 容器网络地址 | 默认宿主机入口 |
|---|---|---|---|
| PostgreSQL（必选） | `postgres:16-alpine` | `database:5432` | 不发布 |
| TeslaMate（必选） | `teslamate/teslamate:4.3.0` | `teslamate:4000` | `http://127.0.0.1:4000` |
| Mileaf Server（必选） | 仓库根目录 Dockerfile | `mileaf:8080` | `http://127.0.0.1:8080` |
| MQTT（可选） | `eclipse-mosquitto:2` | `mosquitto:1883` | 不发布 |
| Grafana（可选） | `teslamate/grafana:4.3.0` | `grafana:3000` | `http://127.0.0.1:3000` |

Compose 自动创建本站点的 bridge 网络，组件通过服务名互相访问，无需预建外部网络。
Mileaf 当前仍通过 `mileaf_reader` 只读访问 TeslaMate 数据库；开启 MQTT 不会切换数据来源。
Grafana 使用独立的 `grafana_reader`，额外允许读取面板需要的 `car_settings`。
两个只读账号均不能读取 `private.tokens`。

## 配置、密码与首次使用

默认配置文件为仓库根目录的 `.deployment/.env`，权限为 `0600`，已加入 Git 忽略规则。
使用 `--data-dir` 时，配置保存在指定目录的 `.env` 中。

| 配置字段 | 用途 |
|---|---|
| `COMPOSE_PROJECT_NAME` | 自动生成的站点标识，决定容器、网络和卷的命名前缀 |
| `MILEAF_ACCESS_PASSWORD` | Mileaf 初始访问口令 |
| `GRAFANA_ADMIN_PASSWORD` | Grafana 初始管理员密码，用户名为 `admin` |
| `POSTGRES_PASSWORD` | TeslaMate 数据库账号密码 |
| `MILEAF_READER_PASSWORD` / `GRAFANA_READER_PASSWORD` | 两个只读数据库账号的独立密码 |
| `ENCRYPTION_KEY` | TeslaMate 用于加密其保存的 Tesla 凭据的密钥 |

通过服务器本地编辑器查看初始访问口令，脚本不会在终端打印密码。Mileaf 已完成初始化，
直接使用客户端登录，无需再次调用 `/v1/auth/setup`；健康检查地址为 `/v1/health`。
车辆授权仍需在 TeslaMate 页面完成，部署脚本不收集 Tesla 凭据。

脚本将配置视为受限格式的部署状态文件，不支持添加任意环境变量，也不是密码轮换工具。
请保留生成的密钥、项目名及字段格式，通过脚本参数调整组件、端口和绑定地址。
在应用内修改或另行重置登录密码后，文件中的初始密码不会自动同步；重跑保留已有登录状态。

## 远程访问

默认回环地址仅允许部署机器本地访问。远程服务器可通过 SSH 转发访问：

```bash
ssh -L 4000:127.0.0.1:4000 -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 user@server
```

可信内网可通过 `--bind 192.168.1.10` 绑定服务器的实际内网 IPv4。
`--bind 0.0.0.0` 会向所有网卡发布 HTTP 端口，不应直接将 TeslaMate 页面暴露到公网。
手机长期接入和 HTTPS 反向代理配置见[远程接入说明](../docs/installation.md#4-远程接入二选一)。
Mosquitto 当前仅在容器网络内提供匿名访问；对外接入前需另行配置认证和 TLS。

## 重跑与数据保留

使用相同配置目录重跑，会复用项目名、密码和数据卷，重新构建 Mileaf，并执行安装检查。
在同一个 Docker 主机上，应保留原配置文件；更换配置目录且不带上原配置，会创建新站点。

```bash
# 为已有站点启用可选组件
bash deploy/install.sh --non-interactive --mqtt --grafana

# 停止可选组件，保留其数据卷
bash deploy/install.sh --non-interactive --no-mqtt --no-grafana
```

关闭 MQTT 时，TeslaMate 会设置 `DISABLE_MQTT=true`，仍可采集并存储车辆数据。
省略组件参数时使用已保存的选择；交互运行会再次询问，回车保留当前值。

业务数据实际位于 Docker 命名卷：`database`、`import`、`mileaf-state`、`mqtt-data`、
`grafana-data`，卷名带站点项目名前缀。配置目录不包含这些卷的数据。
备份时应同时保留数据库备份、应用状态卷和 `.env`，尤其是 TeslaMate 的加密密钥。
不要删除配置文件来重装，也不要用 `docker compose down -v` 停止站点，它会删除数据卷。

## 排查问题

安装失败会保留配置与数据，修复原因后使用相同参数重跑。数据库迁移不会自动回滚。
常见原因包括 Docker 未启动、Compose 插件缺失、软件包冲突、端口占用、镜像下载失败，
或已有只读账号被授予了额外权限。

将以下示例中的项目名替换为脚本输出的实际值：

```bash
docker ps -a --filter label=com.docker.compose.project=mileaf-xxxxxxxxxxxx
docker logs --tail 100 mileaf-xxxxxxxxxxxx-teslamate-1
docker logs --tail 100 mileaf-xxxxxxxxxxxx-mileaf-1
```

按 Docker 权限要求添加 `sudo`。分享日志前先脱敏，不要分享 `.env` 或含密码的 Compose 展开结果。
一键脚本使用的是本目录 `stack.compose.yml`；仓库根目录的 `docker-compose.yml`
用于已有 TeslaMate 的独立接入，不能直接替代本脚本的配置。

## 文件职责

| 文件 | 说明 |
|---|---|
| [install.sh](install.sh) | 参数解析、配置保存、Docker 检查与安装、服务编排和就绪检查 |
| [stack.compose.yml](stack.compose.yml) | 完整站点服务、网络、端口、持久卷及可选 profiles |
| [stack-reader.sql](stack-reader.sql) | 一键部署使用的可重复执行账号初始化与权限检查 |
| [mileaf-reader.sql](mileaf-reader.sql) | 首次创建 Mileaf 只读角色，由完整站点初始化脚本复用；手动使用方式见原安装指南 |
| [mosquitto.conf](mosquitto.conf) | 站点容器网络内的 MQTT 监听与持久化配置 |

更完整的站点说明见[一键安装指南](../docs/one-click-installation.md)。脚本不负责
PostgreSQL 大版本迁移；修改上游镜像版本前，先备份并验证 Mileaf 的数据库兼容性。
