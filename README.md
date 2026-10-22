# Mileaf Server（行笺服务端）

TeslaMate 数据的只读 API 层，供 Mileaf Mobile 调用。运行环境为 Python 3.12。

## 本地运行

Docker / compose 安装、只读角色 SQL、远程接入与升级步骤见
[自托管安装指南](docs/installation.md)。

使用 `uv sync` 安装项目依赖。按 `.env.example` 将配置注入环境后，运行
`uv run mileaf-server`；服务只读取环境变量，不自动加载 `.env`。
`GET /v1/health` 匿名检查数据库连接，数据库不可达时返回 503。

数据库应使用只读专用角色，连接池会额外设置只读事务与查询超时。
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
