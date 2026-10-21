# Mileaf Server（行笺）

Mileaf（行笺）是 TeslaMate 数据的移动端体验层。本仓库提供两件事：

1. **Mileaf Cloud**：多租户 FastAPI 服务，拥有自己的数据模型和开放 API，供 Flutter 客户端（MileafMobile）调用。
2. **Mileaf Agent**：部署在用户 TeslaMate 旁边的只读连接器，把 TeslaMate 产生的数据增量推送到云端。

当前是首版 MVP，已覆盖车辆列表、车辆概览、行程、充电、统计和 Agent 配对/同步。

## 架构

```
┌─────────────────────┐
│  Flutter (Mileaf)   │
└──────────┬──────────┘
           │ HTTPS + JWT
┌──────────▼──────────┐        ┌──────────────────────┐
│   Mileaf Cloud      │───────▶│ PostgreSQL (Mileaf)  │
│   FastAPI / 多租户  │        │ 租户、车辆、行程... │
└──────────▲──────────┘        └──────────────────────┘
           │ HTTPS + Agent Token
┌──────────┴──────────┐        ┌──────────────────────┐
│   Mileaf Agent      │───────▶│ TeslaMate PostgreSQL │
│   Docker / 只读     │  只读  │ （用户自己的实例）   │
└─────────────────────┘        └──────────────────────┘
```

### 为什么用 Agent，而不是让云端直连用户数据库

- 国内家庭宽带通常没有公网 IP，云端无法直接访问用户家里的 PostgreSQL。
- 用户不需要暴露数据库端口，Agent 主动出站推送即可。
- Agent 负责 TeslaMate schema 适配，云端只接触 Mileaf 自己的领域模型。
- Agent 使用只读数据库账号，不写 TeslaMate 数据。

## 快速开始

### 1. 本地启动 Cloud

```bash
uv sync
cp .env.example .env
# 修改 .env 中的数据库密码、MILEAF_JWT_SECRET 和 MILEAF_AGENT_TOKEN_PEPPER
# 生产环境设置 MILEAF_ENV=prod，服务会拒绝弱 secret

# 方式 A：Docker 启动 PostgreSQL
docker compose up -d db

# 方式 B：已有 PostgreSQL，直接配置 MILEAF_DATABASE_URL

# 初始化数据库（二选一）
uv run alembic upgrade head
# 或本地开发时设置 MILEAF_AUTO_CREATE_TABLES=true

uv run uvicorn mileaf.main:app --reload
```

打开 <http://localhost:8000/docs> 查看 OpenAPI，Flutter 端可以直接生成 Dart 客户端。

### 2. 注册并创建配对码

```bash
curl -s http://localhost:8000/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"owner@example.com","password":"change-me-please","display_name":"Owner"}'
```

返回 `access_token`。创建 Agent 配对码：

```bash
curl -s http://localhost:8000/v1/agent/pairing-codes \
  -H "authorization: Bearer $ACCESS_TOKEN"
```

### 3. 启动 Agent

Agent 需要能访问 TeslaMate 的 PostgreSQL，以及 Mileaf Cloud。

```bash
docker build -f docker/agent.Dockerfile -t mileaf-agent .

docker run -d --name mileaf-agent --restart unless-stopped \
  -v mileaf-agent-data:/data \
  -e MILEAF_AGENT_TESLAMATE_DATABASE_URL='postgresql://teslamate:password@host.docker.internal:5432/teslamate' \
  -e MILEAF_AGENT_MILEAF_URL='http://host.docker.internal:8000' \
  -e MILEAF_AGENT_PAIRING_CODE='ABCD2345' \
  -e MILEAF_AGENT_SOURCE_NAME='Home TeslaMate' \
  mileaf-agent

# 先手动跑一次，确认配对和同步成功（用同一个 env 文件）
docker run --rm --env-file agent.env -v mileaf-agent-data:/data \
  mileaf-agent mileaf-agent run --once
```

Agent 支持的配置见 `.env.example` 的 `MILEAF_AGENT_*` 部分。配对成功后 token 会保存到 `/data/agent-state.json`（权限 `0600`），后续不再需要配对码。

Linux 上如果 Mileaf Cloud 跑在宿主机，把 `host.docker.internal` 换成宿主机 IP，或加：

```bash
--add-host=host.docker.internal:host-gateway
```

### 4. 给 Agent 创建只读数据库账号

不要用 TeslaMate 的写账号。建议在 TeslaMate 的 PostgreSQL 中执行：

```sql
CREATE ROLE mileaf_reader LOGIN PASSWORD 'replace-me';
GRANT CONNECT ON DATABASE teslamate TO mileaf_reader;
GRANT USAGE ON SCHEMA public TO mileaf_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mileaf_reader;
-- 让 TeslaMate 以后新建的表也自动可读（需要用 TeslaMate 的建表角色执行）
ALTER DEFAULT PRIVILEGES FOR ROLE teslamate IN SCHEMA public
  GRANT SELECT ON TABLES TO mileaf_reader;
```

然后 Agent 的 `MILEAF_AGENT_TESLAMATE_DATABASE_URL` 使用 `mileaf_reader`。Agent 只执行 `SELECT`，不会修改 TeslaMate 数据。

### 5. 查询数据

```bash
curl -s http://localhost:8000/v1/vehicles \
  -H "authorization: Bearer $ACCESS_TOKEN" \
  -H "X-Tenant-ID: $TENANT_ID"
```

`X-Tenant-ID` 可省略，省略时使用当前用户的第一个租户。

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/auth/register` | 注册，创建用户 + 默认租户 |
| POST | `/v1/auth/login` | 登录 |
| POST | `/v1/auth/refresh` | 刷新 access token |
| GET | `/v1/auth/me` | 当前用户 |
| GET | `/v1/auth/tenants` | 当前用户的租户 |
| GET | `/v1/vehicles` | 车辆列表 + 最新状态 |
| GET | `/v1/vehicles/{id}` | 车辆详情 |
| GET | `/v1/vehicles/{id}/summary?period=30d` | 概览（状态 + 统计） |
| GET | `/v1/vehicles/{id}/stats?period=30d` | 里程/能耗/充电统计 |
| GET | `/v1/vehicles/{id}/drives` | 行程列表（游标分页） |
| GET | `/v1/vehicles/{id}/drives/{drive_id}` | 行程详情 |
| GET | `/v1/vehicles/{id}/charges` | 充电列表（游标分页） |
| GET | `/v1/vehicles/{id}/charges/{charge_id}` | 充电详情 |
| POST | `/v1/agent/pairing-codes` | 创建一次性配对码 |
| POST | `/v1/agent/pair` | Agent 用配对码换长期 token |
| POST | `/v1/agent/sync` | Agent 批量推送数据 |
| POST | `/v1/agent/heartbeat` | Agent 心跳 |
| GET | `/v1/agent/sources` | 数据源列表 |
| DELETE | `/v1/agent/sources/{id}` | 撤销数据源 |

`period` 支持 `7d`、`30d`、`90d`、`month`、`year`、`all`。所有距离/能量/温度使用 SI 单位，客户端按用户偏好转换。

## 数据同步行为

- Agent 首次连接做全量同步，之后按行程/充电的 `start_date` 增量同步，并保留 2 天重叠窗口，避免 TeslaMate 事后修正记录（例如补录充电费用）丢失。
- 每 24 小时自动做一次全量同步（可配置）。
- 服务端按 `(data_source_id, external_id)` 幂等 upsert，重复推送不会产生重复数据。
- Agent 每次都会同步车辆和最新状态；行程/充电按批次（默认 200）推送。
- 不保存原始 GPS 轨迹点。当前 API 只提供行程/充电的起终点和统计；轨迹图和功率曲线是下一阶段。
- TeslaMate 中删除的记录不会自动从云端删除；下一阶段会增加墓碑或全量对账。
- 不提供 TeslaMate 数据库里不存在的实时字段（锁车、哨兵、充电上限）；这些需要 Tesla 官方 API，当前版本不接入。

## 开发

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
```

数据库迁移：

```bash
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "change description"
```

## 目录

```
src/mileaf/
  api/           # 认证、车辆、Agent 路由
  services/      # 数据入库、统计、分页
  agent/         # 用户侧只读连接器
  models.py      # Mileaf 自己的数据模型
  schemas.py     # Pydantic / OpenAPI 契约
  security.py    # 密码、JWT、Agent token
tests/           # API、租户隔离、Agent 映射/同步测试
migrations/      # Alembic
docker/          # Cloud / Agent 镜像
```

## 合规与风险提示（重要）

这一版按“闭源商业 + 只读用户 TeslaMate 数据 + 国内部署”实现，但以下风险需要在商业上线前由你和法务确认：

### TeslaMate 许可与集成边界

- TeslaMate 是 **AGPL-3.0-or-later 加附加条款**。其 README 明确表示不欢迎闭源产品使用其组件，并写明“与 TeslaMate 集成的软件只能通过 MQTT”。
- TeslaMate 官方 FAQ 明确：**没有公开 API，数据库不是接口**，schema 可能随时变化。
- 本项目没有复制 TeslaMate 代码或 Grafana dashboard JSON，API 完全重写，只读取用户授权的数据；但这不当然消除许可和商标风险。建议在商业发布前联系 TeslaMate 项目取得书面许可，或让律师评估。
- 不要在商业产品名称、Logo、域名、应用标识中使用 `TeslaMate` 商标。产品名保持 Mileaf / 行笺。

### 国内数据合规

- 车辆精确位置和 VIN 属于敏感/个人信息。需要隐私政策、单独同意、最小化采集、访问审计、加密存储、保留期限和删除/导出能力。当前 VIN 明文存储且不返回给移动端，正式上线前建议加密或只保留后 6 位。
- 境内运营通常需要 ICP 备案；如涉及地图展示/测绘相关服务，需评估相关资质要求。
- 建议数据存储在中国大陆境内，避免跨境传输；如使用境外云，需评估个人信息出境合规路径。
- 正式上线前补充：短信/手机号登录、实名/风控（如适用）、限流、审计日志、数据删除工单、等保/安全评估（按业务规模）。

## 已知限制 / 下一步

- 只有邮箱密码登录，没有短信、OAuth、找回密码。
- 没有刷新 token 吊销列表；生产环境建议加 Redis 黑名单或短期 access token + 轮换 refresh token。
- 没有原始轨迹点、功率曲线、电池健康、预估续航、Vampire Drain（驻车耗电）、时间线、地图聚合、软件更新记录。
- Agent 只适配当前 TeslaMate schema；后续应增加版本探测和按版本适配层。
- 统计按 UTC 计算；按用户时区分桶、周/月报表需要后续实现。
- 未接入计费、配额、管理后台、告警和可观测性后端。
