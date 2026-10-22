# Mileaf Server（行笺服务端）

TeslaMate 数据的只读 API 层，供 Mileaf Mobile 调用。运行环境为 Python 3.12。

## 本地运行

使用 `uv sync` 安装项目依赖。按 `.env.example` 将配置注入环境后，运行
`uv run mileaf-server`；服务只读取环境变量，不自动加载 `.env`。
`GET /v1/health` 匿名检查数据库连接，数据库不可达时返回 503。

数据库应使用只读专用角色，连接池会额外设置只读事务与查询超时。
当前只提供运行时骨架，不包含认证或业务端点。

## 验证

运行 `uv run ruff check`、`uv run mypy` 和 `uv run pytest`。
数据库集成测试使用预先拉取的 `postgres:16-alpine` 镜像和 Docker。
