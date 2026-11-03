# ADR-0002: 只读直连 TeslaMate 数据库

- 状态：部分由 [ADR-0004](0004-limited-charging-cost-writes.md) 替代（有限费用补录），其他约束继续生效
- 日期：2026-10-21

## 背景

Mileaf 要在移动端复现 TeslaMate 在 Grafana 里的数据体验。数据已经在用户的 TeslaMate PostgreSQL 里，且 TeslaMate 会持续写入。可选的取数方式有：

1. 云端托管 + 用户侧 Agent 增量推送（上一版方案）。
2. 直连用户 TeslaMate 数据库，只读查询。
3. 用户手动导出/定期 ETL 到独立库。

方案 1 要求 Mileaf 持有用户数据副本，且为了对齐 schema 需要维护同步链路和中间存储；方案 3 无法做到「打开 App 就是最新数据」，且用户体验成本高。商业化上，Tesla 凭据（TeslaMate `tokens` 表）属于高风险资产，Mileaf 不应接触任何副本。

## 决定

1. **直连只读**：Mileaf Server 通过 PostgreSQL 连接用户的 TeslaMate 库，只执行 `SELECT`。
2. **零结构变更**：不在 TeslaMate 库中建表、建视图、建触发器、加索引、写入任何业务数据（含 migration 记录）。
3. **不接触凭据**：专用数据库角色对 `tokens` 表无任何权限；代码中不出现对该表的引用；启动探针验证 `SELECT` 该表被拒绝。
4. **权限模型**：用户创建 `mileaf_reader` 角色，仅授予 `CONNECT`、`USAGE` 和所需表的 `SELECT`。文档提供完整 SQL。
5. **Schema 兼容**：以 Ecto 的 `schema_migrations` 最大版本号作为兼容判据，声明支持区间；启动时校验必需表/列，不匹配则拒绝启动并给出可读错误，不静默降级。
6. **不从 Grafana 复制查询**：TeslaMate 以 AGPL-3.0 分发，dashboard 里的 SQL/JSON 不直接复制；按 `drives`、`charging_processes` 等表的语义重写，并在代码中注明口径来源。
7. **查询约束**：每个请求设置 `statement_timeout`；列表接口强制分页；位置点序列只用于详情图表，按目标点数在 SQL 内降采样，不整表回传。
8. **口径与单位**：API 一律返回公制（km、kWh、kW、°C、Wh/km）和 UTC 时间；单位换算与时区渲染由客户端负责，避免服务端出现第二套口径。
9. **新鲜度可见**：返回数据时附带 `freshness`（最近位置点/状态时间）。TeslaMate 休眠或停摆导致的数据空洞如实呈现，不插值补齐。

## 影响

- 服务端没有自己的业务数据库，因此不能做历史快照、跨站点对比、账号体系。
- 重聚合查询会占用用户 TeslaMate 库的资源，需要超时和并发上限；必要时在进程内做短 TTL 缓存（60 秒级），不引入第二个数据库。
- 与 TeslaMate schema 强耦合：上游升级可能破坏兼容，必须维护版本矩阵和探针。
- 无法提供车辆控制类功能（开锁、空调等），因为那需要 Tesla token。
- 天然规避了「云端保存用户行车轨迹和 Tesla 凭据」的合规与商业风险。
