# 一键安装完整站点

适用于一台机器部署一个新站点：必选 TeslaMate、PostgreSQL、Mileaf Server，
可选 MQTT（Mosquitto）和 TeslaMate Grafana。所有服务加入同一个 Compose bridge 网络，
通过 `database`、`teslamate`、`mileaf`、`mosquitto`、`grafana` 名称通信。
此方案沿用 Mileaf 当前的只读数据库适配器，开启 MQTT 不会改变 Mileaf 的数据来源。
已有 TeslaMate 的用户继续使用[原安装指南](installation.md)，避免另建空站点。

## 执行

将完整仓库放到目标服务器，进入仓库根目录运行：

```sh
bash deploy/install.sh
```

脚本询问是否安装 MQTT 和 Grafana，首次默认都不安装。无人值守方式：

```sh
# 三个必选服务
bash deploy/install.sh --non-interactive --no-mqtt --no-grafana

# 全部五个服务
bash deploy/install.sh --non-interactive --mqtt --grafana
```

自动安装 Docker 支持使用 systemd 的 Ubuntu/Debian 官方发行版（amd64/arm64），
通过 [Docker 官方 apt 仓库](https://docs.docker.com/engine/install/ubuntu/)安装
Engine、Buildx、Compose，需要 root 或 sudo。不会自动卸载已有容器运行时，也不修改用户组。
无人值守安装 Docker 时需要以 root 运行或预先配置可用的免密码 sudo。
已有 Docker 的 Linux/macOS 可直接运行；macOS 缺少 Docker 时需先安装并启动 Docker Desktop。
已有 Engine 但缺少 Compose 时，按该发行版官方指南安装 Compose 插件后重跑。
脚本要求 Compose v2.20+，支持 `up --wait --wait-timeout`。

服务器需要持续联网，能访问 Docker Hub、Python/uv 镜像及依赖源、Tesla 服务；
建议至少 2 GB 内存，构建期间需要额外余量。宿主机不需要安装 Python 或 uv。
TeslaMate/Grafana 固定为 `4.3.0`，PostgreSQL 使用项目现有测试基线 `16-alpine`，
Mosquitto 使用 `2` 系列；Mileaf 由本仓库 Dockerfile 构建，不依赖尚未发布的 Mileaf 镜像。

## 安装结果与访问

默认绑定 `127.0.0.1`：

| 服务 | 地址或用途 |
|---|---|
| TeslaMate | `http://127.0.0.1:4000`，完成 Tesla 车辆授权 |
| Mileaf | `http://127.0.0.1:8080`，健康检查 `/v1/health` |
| Grafana（选装） | `http://127.0.0.1:3000`，用户名 `admin` |
| PostgreSQL | 仅容器网络内的 `database:5432` |
| MQTT（选装） | 仅容器网络内的 `mosquitto:1883` |

首次配置保存在仓库 `.deployment/.env`，权限为 `0600`，已被 Git 忽略。
脚本生成独立数据库密码、只读账号密码、TeslaMate 加密密钥及应用访问密码，终端不打印密码。
在服务器本地用编辑器打开该文件，获取 `MILEAF_ACCESS_PASSWORD` 登录 Mileaf，
获取 `GRAFANA_ADMIN_PASSWORD` 登录 Grafana。Mileaf 已初始化，不需要再次调用 `/v1/auth/setup`。
如果之后重置应用口令，配置里的初始密码不会自动同步；重跑脚本保留已有口令。

远程服务器可先用 SSH 转发完成访问（无需开放这些公网端口）：

```sh
ssh -L 4000:127.0.0.1:4000 -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 user@server
```

手机长期接入可使用[原指南中的 Tailscale 或 HTTPS 反向代理](installation.md#4-远程接入二选一)。
仅在可信内网直接访问时，可绑定服务器的内网 IPv4，例如：

```sh
bash deploy/install.sh --bind 192.168.1.10 --mqtt --grafana
```

`--bind 0.0.0.0` 会向所有网卡发布 HTTP 端口；TeslaMate 页面包含车辆授权入口，
不要直接暴露到公网。MQTT 当前为站点容器网络内匿名访问，外接客户端前需另行配置认证和 TLS。
未安装 MQTT 时会设置 TeslaMate 的 `DISABLE_MQTT=true`，仍可正常采集并写入数据库。
参考 [TeslaMate 环境变量](https://docs.teslamate.org/docs/configuration/environment_variables/)。

## 重跑、可选组件和数据

相同配置目录对应相同的随机项目名、密钥、命名卷与网络。重跑会重新构建 Mileaf、
复用已有数据，执行 TeslaMate 官方迁移入口，检查只读角色，再等待服务启动。
Mileaf 使用 `mileaf_reader`，Grafana 使用独立的 `grafana_reader`，只允许读取约定的业务表，
均不能读取 `private.tokens`；Grafana 另有读取 `car_settings` 的权限，供电池与充电面板使用。

```sh
# 后续增加组件；省略参数时使用保存的选项（交互运行会再次询问）
bash deploy/install.sh --non-interactive --mqtt --grafana

# 停止可选组件，保留其数据卷；TeslaMate 同步关闭 MQTT 发布
bash deploy/install.sh --non-interactive --no-mqtt --no-grafana

# 自定义端口/配置路径；以后每次重跑都使用相同 --data-dir
bash deploy/install.sh --data-dir /srv/mileaf-config --mileaf-port 8081

# 只生成配置，不安装 Docker，也不创建容器
bash deploy/install.sh --prepare-only --non-interactive --mqtt --grafana
```

数据库、Mileaf 认证状态、Grafana 状态、MQTT 持久化数据和导入目录分别使用命名卷。
`.deployment` 只存部署配置，不存数据库本体。请同时备份数据库、状态卷和 `.env`；
丢失 TeslaMate 的 `ENCRYPTION_KEY` 会影响已有 Tesla 凭据解密。
不要删除 `.env` 来重装，也不要使用 `docker compose down -v`，后者会删除数据卷。
如使用自定义配置目录，须自行保证不会被其他仓库提交或被其他用户读取。

失败后保留现状并重跑，不会自动回滚数据库迁移或清空卷。服务日志可能包含敏感信息，分享前脱敏。
查状态/日志无需展开包含密码的 Compose 配置：

```sh
docker ps --filter label=com.docker.compose.project=<脚本输出的项目名>
docker logs <目标容器名>
```

本脚本用于新站点安装与同版本重部署，不负责 PostgreSQL 大版本迁移。
升级上游版本前应先备份并验证 Mileaf 的 schema 兼容性；镜像 tag 固定不等于 digest 固定。
