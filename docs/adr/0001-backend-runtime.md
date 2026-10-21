# ADR-0001: 后端运行时选型（FastAPI）

- 状态：Accepted
- 日期：2026-10-21

## 背景

Mileaf Server 的职责是把 TeslaMate 的 PostgreSQL 数据转换成移动端友好的只读 API。约束：

- 单租户，一个实例服务一个站点（见 ADR-0003）。
- 端点规模约 15–25 个，几乎全是 GET + 聚合查询。
- 数据量级：家庭单车主，万级行程、十万级位置点；瓶颈在 PostgreSQL 聚合，不在应用运行时。
- 需要给 Flutter 端提供稳定的 OpenAPI 契约。
- 以 Docker 镜像形式分发给用户自行部署（NAS、树莓派、家用服务器，含 ARM64）。

## 决定

采用 **FastAPI**（Python 3.12+）：

- 数据访问用 `asyncpg` 执行手写 SQL，不引入 ORM。Mileaf Server 没有自己的 schema（见 ADR-0002、ADR-0003），ORM 只会多出一层需要跟随 TeslaMate 变化的抽象。
- DTO 用 Pydantic v2 定义，OpenAPI 3.1 自动生成，作为 Flutter 端 Dart 客户端的生成源。
- 依赖用 `uv` 管理，质量门禁为 `ruff` + `mypy` + `pytest`。

## 被否决的选项：Rust（axum + sqlx）

Rust 在单二进制分发、内存占用、sqlx 编译期 SQL 校验上更优；否决原因是当前最大的不确定性是产品口径（指标、聚合方式、图表切法）而非运行时，快速迭代的优先级更高，且 utoipa 的 OpenAPI 生成需要额外手工打磨，会拖慢 Flutter 客户端的契约同步。

重新评估的触发条件：确认要做成「装完不管」的长期分发成品，且镜像体积/内存成为用户实际抱怨点。

## 影响

- 必须自己实现 TeslaMate schema 探针（启动时校验必需表与列、读 `schema_migrations` 版本），弥补运行时类型检查的缺失。这是本项目唯一需要额外补的安全网。
- 镜像基于 Python slim，amd64/arm64 天然可用；RSS 高于 Rust，但在家用 NAS 上可接受。
- API 契约保持语言无关（OpenAPI + 针对 TeslaMate 夹具库的契约测试），保留将来重写的可能。
- 上一版 FastAPI 骨架（`90edb74`）可参考，但架构已不同：没有自有数据库、没有 Agent、没有多租户。
