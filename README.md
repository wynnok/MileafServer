# Mileaf Server（行笺服务端）

TeslaMate 数据的默认只读 API 层，供 Mileaf Mobile 调用；显式启用后允许补录已结束充电会话的缺失费用
（[ADR-0004](docs/adr/0004-limited-charging-cost-writes.md)）。运行环境为 Python 3.12。

## 本地运行

Docker / compose 安装、只读角色 SQL、远程接入与升级步骤见
[自托管安装指南](docs/installation.md)。

新建完整站点可运行 `bash deploy/install.sh`，一键部署 TeslaMate、PostgreSQL 和 Mileaf，
交互选择 MQTT / Grafana；Docker 自动安装范围、配置与使用方法见
[一键安装指南](docs/one-click-installation.md)。

使用 `uv sync` 安装项目依赖。按 `.env.example` 将配置注入环境后，运行
`uv run mileaf-server`；服务只读取环境变量，不自动加载 `.env`。
`GET /v1/health` 匿名检查数据库连接，数据库不可达时返回 503。

数据库应使用只读专用角色，连接池会额外设置只读事务与查询超时；仅在启用充电费用补录时，
额外授予 `charging_processes.cost` 的列级 `UPDATE` 并设置 `MILEAF_CHARGING_COST_WRITES_ENABLED=true`。
已提供健康检查、单站点认证、元信息，以及车辆、概览、行程、充电会话与统计端点；
单位与缓存原语由这些端点复用。

首次调用 `POST /v1/auth/setup`，JSON 请求体为 `{"password":"YOUR_ACCESS_PASSWORD"}`，
口令至少 10 字符；随后用相同请求体调用 `POST /v1/auth/login` 获取 `token` 与
`expires_at`。其余 API 请求携带 `Authorization: Bearer <token>`；健康检查与认证
接口匿名。token 有效期为 30 天，最多保留 10 个。同一来源 IP 每分钟最多允许
5 次失败登录；反向代理应正确传递受信任的客户端地址。

`MILEAF_STATE_FILE` 指向挂载卷中的 `state.json`，以 `0600` 权限保存 Argon2id
口令哈希及 token 摘要。写入采用同目录 `O_EXCL` 临时文件和原子 rename；同目录的
空 `state.json.lock` 用于跨进程互斥，避免并发初始化或登录覆盖状态。业务数据不落盘。
重置口令需停止所有实例后删除 `state.json`，再启动并重新 setup；全部旧 token 随之失效。

启动时读取并校验 `settings.unit_of_length`，缓存到当前连接池生命周期；修改展示设置
后需重启服务。数据库暂不可达时，健康检查仍可返回 503，恢复后的首次单位读取必须
完成同一校验。距离和速度列已为公制，`mi` 展示偏好不会触发二次换算，详见
[接口口径](docs/api/v1-surface.md#单位归一化与口径来源)。

进程内缓存默认 TTL 为 30 秒、容量为 256 条，可通过 `MILEAF_CACHE_TTL_SECONDS`
与 `MILEAF_CACHE_CAPACITY` 配置；TTL 设为 0 关闭服务端缓存。缓存命中不改写
`freshness`。概览、行程与充电端点使用进程内缓存并声明自身 TTL；统计每次重新聚合。

## 验证

运行 `uv run ruff check`、`uv run mypy` 和 `uv run pytest`。
数据库集成测试使用预先拉取的 `postgres:16-alpine` 镜像和 Docker。

## 接口增量与冻结契约

除 v1 面（[接口口径](docs/api/v1-surface.md)）外，已交付三批增量（默认只读），各自带冻结 OpenAPI 快照：

- `0.2.0` 充电体验（覆盖、配对均价、地点账目、过程质量）：[契约](docs/product/charging-experience-spec.md)、[openapi-0.2.0.json](docs/api/openapi-0.2.0.json)；
- `0.3.0` 统计洞察（驾驶覆盖、用车节奏、温度关联、额定续航、闭合停放窗口）：[openapi-0.3.0.json](docs/api/openapi-0.3.0.json)、[验证记录](docs/testing/statistics-insights.md)；
- `0.4.0` 车主体验（电量历史、环境与胎压、地点回顾、补能习惯、行驶过程、固件履历）与可选的充电费用补录：[契约](docs/product/owner-experience-spec.md)、[openapi-0.4.0.json](docs/api/openapi-0.4.0.json)、[验证记录](docs/testing/owner-experience.md)。

`docs/mobile-api-proposals/` 下的三份交接文档保留为需求来源与历史依据，其中的“待实现”表述不代表当前能力缺失；
运行时能力以 `/v1/meta` 的声明和各版本冻结 OpenAPI 为准。真实站点与 Grafana 对账仍未执行，见 issue #28。

## 参与贡献

开发环境、质量门禁、PR 约定与许可证边界见 [贡献指南](CONTRIBUTING.md)；
漏洞请按 [安全策略](SECURITY.md) 走私密报告，不要开公开 issue。

## 许可证

[GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0）。

Copyright (C) 2026 wynnok
