# 贡献指南

感谢你愿意为 Mileaf Server 出力。本文说明提 issue、改代码、开 PR 的约定。

## 开始之前

- 先读 [GLOSSARY.md](GLOSSARY.md)（领域词汇的唯一来源）与 [docs/adr/](docs/adr/)（架构决定）；
  不要在代码、文档或 issue 里引入同义词。
- 守住 ADR-0002 / ADR-0004 的边界：默认只读，唯一允许的业务写入是补录已结束充电会话的缺失费用
  （只更新 `charging_processes.cost`）。不要提交读取 Tesla 凭据、修改 TeslaMate schema、
  批量写入、覆盖已有费用或开放车辆控制的改动。
- 不确定该不该开票、怎么开票，见 [issue tracker 约定](docs/agents/issue-tracker.md)。
- 参与本项目即表示你同意遵守仓库的 [AGPL-3.0 许可证](LICENSE)。

## 开发环境

需要 Python 3.12 与 [`uv`](https://docs.astral.sh/uv/)：

```bash
uv sync
uv run mileaf-server        # 按 .env.example 注入环境变量；服务只读环境变量，不自动加载 .env
```

质量门禁（请全部在本地跑通再提 PR）：

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
```

- 数据库集成测试走 HTTP/ASGI 与**新建隔离 PostgreSQL**，需要 Docker 与预拉取的 `postgres:16-alpine`
  镜像；没有 Docker 时这些用例会 skip，不代表通过。
- 性能冒烟与大规模夹具用 `uv run pytest -m slow` 单独运行。
- 新增或修改接口时，必须同步更新 [docs/api/v1-surface.md](docs/api/v1-surface.md)、
  所属能力的冻结 OpenAPI 快照，以及对应 `docs/product/` 契约或 `docs/testing/` 验证记录。

## 提 PR

- 一个 PR 只解决一个 issue，正文开头写 `Closes #<n>`；标题用 `impl:` / `fix:` / `docs:` / `test:` 前缀。
- 必须给出三条门禁与 `uv run pytest` 的结果；涉及数据库时说明用的是新建隔离库，
  而不是任何真实站点或生产连接。
- **禁止**提交生产 DSN、口令、token、`state.json`、真实站点数据或任何凭据。
  被 `.gitignore` 挡住的本地文件不要用 `-f` 绕过。
- 外部 PR 与 issue 走同一套 triage 流程与标签，见 [triage labels](docs/agents/triage-labels.md)。
- 真实站点与 Grafana 对账（issue #28）由维护者在真实部署上完成，不接受用合成夹具冒充。

## 许可证

本项目以 **AGPL-3.0-or-later** 发布。你提交的贡献默认按同一许可证授权（inbound = outbound），
无需签署 CLA；请只提交你有权授权的代码。引入第三方代码或数据时，请在 PR 中说明来源与许可证，
并确认与 AGPL-3.0 兼容。

Copyright (C) 2026 wynnok
