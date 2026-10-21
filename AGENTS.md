# MileafServer（行笺服务端）

TeslaMate 数据的只读 API 层，供 Flutter 应用 MileafMobile 调用。不修改 TeslaMate 数据库结构，不读取或保存 Tesla 凭据。

- 当前阶段：设计文档，代码尚未落地。先读 `docs/product/v1-scope.md` 和 `docs/adr/`。
- 技术栈：Python 3.12 + FastAPI + asyncpg（见 `docs/adr/0001-backend-runtime.md`）；没有自有业务库，不写 TeslaMate。
- 数据来源与边界见 `docs/adr/0002-read-only-teslamate-adapter.md`。
- 领域词汇以 `GLOSSARY.md` 为准，不要引入同义词。

## Agent skills

### Issue tracker

GitHub issues in `wynnok/MileafServer`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` plus `docs/adr/`. See `docs/agents/domain.md`.
