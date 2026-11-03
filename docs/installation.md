# 自托管安装与升级

本指南面向已有 TeslaMate 的站点，基准为 v4.3.0。Mileaf 只读 public 业务表，
不读取凭据，不修改 TeslaMate 的表、索引或已有 service。首次部署前保留现有配置备份。

## 1. 创建专用数据库角色

在 TeslaMate 数据库上以数据库管理员身份执行随仓库交付的完整 SQL：

```sh
docker compose exec -T database psql -v ON_ERROR_STOP=1 -U teslamate -d teslamate < deploy/mileaf-reader.sql
docker compose exec database psql -U teslamate -d teslamate
```

在交互式 psql 内执行 `\password mileaf_reader`，按提示设置独立数据库密码。
`database`、管理员名与数据库名需替换成现有 compose 的实际名称。
[SQL 文件](../deploy/mileaf-reader.sql) 只用于首次创建角色：授予 CONNECT、public 的
USAGE 和逐表 SELECT，显式撤销 private.tokens 与 private schema 权限，并设置只读默认值。
不要将已有管理员账号重命名或作为 reader；重复安装时复用已创建角色。

启动探针必须确认 private.tokens 不可读。若现有库通过 PUBLIC 或其他角色授予了凭据读取权限，
单独对 reader 的 REVOKE 无法抵消它；需管理员先检查并调整原有授权，再启动 Mileaf。
指南中的 REVOKE 不会自动修改其他角色或 PUBLIC 的共享权限。

## 2. 使用 Compose 部署

从本仓库根目录构建当前主机架构的本地镜像：

```sh
BUILDX_CONFIG=.cache/buildx docker build -t mileaf-server:local .
```

Dockerfile 使用 Python 3.12 slim、多阶段构建和 `uv sync --frozen --no-dev --no-editable`。
amd64 与 arm64 构建可分别加 `--platform linux/amd64`、`--platform linux/arm64`。
缓存及 buildx 客户端元数据保留在项目内；不安装宿主机全局依赖。
镜像仅复制白名单构建输入，运行阶段只有已安装应用及运行依赖，以 UID/GID 10001 运行。

仓库只提供 [docker-compose.yml](../docker-compose.yml)，默认独立部署并加入已有的
`docker_bridge_net` 外部网络，不发布宿主机端口。数据库与容器反向代理应能通过该网络
访问 Mileaf；如果现有网络名称不同，请同步修改 service 与顶层 `networks` 定义。
数据库主机名使用共享网络内可解析的名称。

若需追加到现有 TeslaMate compose，可复制其中的 `mileaf` service 与 `mileaf-state`
顶层卷定义，按现有网络调整 service 的 `networks`。使用已构建镜像时可移除 `build: .`；
需要构建时，该路径必须指向本仓库根目录。

在 compose 环境中设置 `MILEAF_DATABASE_DSN`：

```text
postgresql://mileaf_reader:YOUR_URL_ENCODED_DB_PASSWORD@database:5432/teslamate
```

密码中的特殊字符须按 URL 编码。可使用受限权限的部署环境文件供 compose 插值；不要提交真实
凭据，也不要将含凭据的 compose 展开结果或容器配置发到日志。应用本身不自动读取 `.env`。
配置默认使用本地 `mileaf-server:local`；若自行管理镜像仓库，通过 compose 的
`MILEAF_IMAGE` 选择已发布的具体版本（本项目在此阶段不发布镜像仓库）。

```sh
docker compose up -d --wait mileaf
docker compose ps mileaf
docker compose exec -T mileaf python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/v1/health').read().decode())"
```

就绪后健康状态为 healthy，响应为 `{"status":"ok","version":"..."}`。
健康检查会实际访问数据库；缺少必需 DSN 时进程退出，错误仅列出变量名。
命名卷首次创建时沿用镜像 `/data` 的 UID/GID 10001 所有权；自备 bind mount 则需提前
让 UID/GID 10001 可写。

## 3. 初始化、登录与重置

首次连接时通过客户端的初始化界面或 HTTP 客户端向 `/v1/auth/setup` 发送
`{"password":"YOUR_ACCESS_PASSWORD"}`（至少 10 字符），再以相同请求体调用
`/v1/auth/login`。后者返回 token 与 expires_at；后续请求使用 `Authorization: Bearer <token>`。
这是 Mileaf 访问口令，与数据库密码和 Tesla 凭据无关。生产操作应通过下节的受保护入口完成。

状态卷只保存口令哈希、token 摘要和时间戳；`state.json` 权限为 0600。
保留该卷即可跨容器重建登录。重置会使全部旧 token 失效：先停止所有 Mileaf 实例，
在卷中仅删除 `state.json`（保留其余文件），再启动并重新 setup。
使用下面命令前确认 compose 指向本站点的卷；此操作不可恢复原 token。

```sh
docker compose stop mileaf
docker compose run --rm --no-deps --entrypoint python mileaf -c "from mileaf.config import Settings; Settings().state_file.unlink(missing_ok=True)"
docker compose up -d mileaf
```

不要用 `docker compose down -v` 重置口令，它可能连带删除 TeslaMate 数据卷。

## 4. 远程接入（二选一）

**Tailscale（推荐）**：在宿主机与手机上自行安装 Tailscale，加入同一 tailnet，限制只有
自己的设备可访问服务。在 `mileaf` service 中加入以下回环端口绑定，再执行
`docker compose up -d mileaf`，并在宿主机启用 Serve：

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

```sh
tailscale serve --bg http://127.0.0.1:8080
```

手机使用命令输出的 tailnet HTTPS 地址连接 Mileaf。Serve 的 HTTPS 需要 tailnet 启用对应
DNS/证书功能；不要启用 Funnel。若只用 tailnet HTTP，可将 compose 发布地址改为宿主机的
Tailscale IP，通过 `http://<tailscale-ip>:8080` 连接，并由 tailnet 访问规则限制来源。
参见 [Tailscale Serve 官方说明](https://tailscale.com/docs/reference/tailscale-cli/serve)。

**公网 IP + 自有 TLS**：自备域名或客户端信任且与访问名称匹配的证书，由现有反向代理在
443 终止 TLS。宿主机代理需添加上述回环端口绑定，再转发至 `http://127.0.0.1:8080`；
公网仅开放代理入口，不开放容器的 8080。
例如在已有 Nginx TLS server 中添加：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
}
```

Mileaf 不负责证书、反向代理、Tailscale 安装或隧道。若 TLS 代理运行在另一个容器，
通过共享网络转发 `http://mileaf:8080`，无需额外向公网发布该端口。

### 独立 compose 与现有 Nginx 共享网络

仓库根目录的 `docker-compose.yml` 适用于已创建 `docker_bridge_net` 的服务器。
将代码放入 `/root/apps/dockerApps/mileaf-server`，在该目录的 `.env` 中设置
`MILEAF_DATABASE_DSN` 和 `MILEAF_TRUSTED_PROXY_IP`（Nginx 在共享网络上的 IP），
并将 `.env` 权限设为 `0600`。执行 `docker compose up -d --build --wait`。
该配置不发布宿主机端口，Nginx 应通过 `http://mileaf:8080` 代理。
`MILEAF_TRUSTED_PROXY_IP` 仅用于 Uvicorn 信任该代理传入的客户端 IP；Nginx 应将
`X-Forwarded-For` 覆盖为 `$remote_addr`，Nginx 容器 IP 改变后需同步更新并重建 Mileaf。
首次部署应先在容器网络内完成访问口令初始化，再启用公网代理，避免初始化接口被抢先调用。

## 5. 完整环境变量

与 `mileaf.config.Settings` 对应，不新增运行期配置：

Compose 显式传入 DSN、状态路径、监听地址与代理信任 IP；其余可选项需要按需加入 mileaf service 的
`environment` 才会传入容器，仅设置宿主机的 compose 插值环境不会自动注入它们。

| 变量 | 必填 / 默认 | 说明 |
|---|---|---|
| MILEAF_DATABASE_DSN | 必填 | 专用 reader 的 PostgreSQL URL |
| MILEAF_STATE_FILE | `/data/state.json` | 持久状态路径，父目录必须可写 |
| MILEAF_LISTEN_ADDRESS | `0.0.0.0:8080` | 容器监听地址；改变端口时同步修改 compose 的目标端口；容器内使用通配地址或 localhost |
| MILEAF_LOG_LEVEL | `INFO` | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| MILEAF_MAX_CONCURRENCY | `4` | 连接上限，至少 1 |
| MILEAF_CACHE_TTL_SECONDS | `30` | 进程内缓存 TTL，0 关闭；统计不使用进程内缓存 |
| MILEAF_CACHE_CAPACITY | `256` | 缓存条目上限，至少 1 |

`MILEAF_IMAGE` 是 compose 的镜像选择变量，不是应用配置；其默认值为 `mileaf-server:local`。
统计响应始终使用 `Cache-Control: private, max-age=30`，查询结果每次重新聚合。

## 6. 升级与回退

自行发布镜像的部署：将 MILEAF_IMAGE 指向新版本，执行 `docker compose pull mileaf`
和 `docker compose up -d mileaf`。本地构建的部署：更新代码与 uv.lock 后重新运行上面的
build 命令及 `up -d`。均保留 state 卷，不运行 TeslaMate migration。
登录后访问 `/v1/meta` 查看 Mileaf 版本、TeslaMate schema 版本与 tested 标记；
结构兼容但未经验证的版本为 `tested=false`，缺少必需结构则拒绝启动。
升级后确认 healthy；需要回退时将镜像切回保留的旧版本并重建 service。

## 交付验证

先构建 `mileaf-server:local`，再运行 `UV_CACHE_DIR=.cache/uv uv run pytest tests/test_deployment.py`。
测试使用预拉取的 postgres:16-alpine、合成数据库和独立 compose 项目，不访问真实 TeslaMate。
未构建本地镜像时部署测试会跳过；其余测试不依赖该镜像。
构建方法参考 [uv Docker 官方指南](https://docs.astral.sh/uv/guides/integration/docker/)。


> 2026-11-03 边界更新：默认只读，显式启用后允许补录已结束充电会话的缺失费用；唯一写入为 charging_processes.cost。规则、接口与最小权限见 docs/adr/0004-limited-charging-cost-writes.md，替代本文绝对只读的描述。
