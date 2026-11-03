# MileafServer（行笺服务端）

TeslaMate 数据的默认只读、有限费用补录 API 层，供 Flutter 应用 MileafMobile 调用。不修改 TeslaMate 数据库结构，不读取或保存 Tesla 凭据。

- 当前阶段：v1 后端实现已完成（#7–#27），等待 #28 与 Grafana 对账验收。产品范围见 `docs/product/v1-scope.md`，边界见 `docs/adr/`。
- 技术栈：Python 3.12 + FastAPI + asyncpg（见 `docs/adr/0001-backend-runtime.md`）；没有自有业务库，仅按 ADR-0004 补录充电费用。
- 数据来源与边界见 `docs/adr/0002-read-only-teslamate-adapter.md`。
- 领域词汇以 `GLOSSARY.md` 为准，不要引入同义词。

## Agent skills

### Issue tracker

GitHub issues in `wynnok/MileafServer`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` plus `docs/adr/`. See `docs/agents/domain.md`.


> 2026-11-03 边界更新：默认只读，显式启用后允许补录已结束充电会话的缺失费用；唯一写入为 charging_processes.cost。规则、接口与最小权限见 docs/adr/0004-limited-charging-cost-writes.md，替代本文绝对只读的描述。
