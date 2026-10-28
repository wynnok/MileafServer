# 充电体验增量契约

- 日期：2026-10-28
- 状态：Implemented locally（2026-10-28；维护者确认后实现，隔离测试及双轴审查通过；未推送/合并/部署）
- 输入：`docs/mobile-api-proposals/charging-experience.md`（设计交接，保留原文）
- 基线：`72710ff6ea5ee2805ed4a9fdea2854b384a44147`；现有 spec #4/#5、实施 #23/#26
- Tracker：增量 spec [#29](https://github.com/wynnok/MileafServer/issues/29)；实施 [#30](https://github.com/wynnok/MileafServer/issues/30) CH01、[#31](https://github.com/wynnok/MileafServer/issues/31) CH02、[#32](https://github.com/wynnok/MileafServer/issues/32) CH03、[#33](https://github.com/wynnok/MileafServer/issues/33) CH04–CH05。#32 阻塞于 #30/#31；#33 阻塞于 #30。
- 生命周期：Draft → Accepted → Implemented（隔离测试及代码审查通过）；真实 Grafana 验收仍由 #28 承担，Implemented 不代表生产对账完成。

## Problem Statement

车主需要知道所选期间的充电费用、补能与记录时长是否完整，并能从结束日日桶找到同一批充电会话、按稳定地点核对账目。现有功率序列经过采样，不能用于计算原始峰值、记录平均功率或记录跨越 20–80 的用时。

## Solution

CH01–CH05 在现有只读 API 上做加法；CH06、地图空间聚合与跨会话性能比较延后。保持旧字段及默认日期依据，增加明确的覆盖、配对均价、统一查询、地点账目及过程质量。新 OpenAPI 冻结后 Mobile 才接入。

## User Stories

1. 作为车主，我想区分免费与费用未知，以免把缺失记录当作省钱。
2. 作为车主，我想看到费用、入电与时长的有效记录数，以便判断合计覆盖。
3. 作为车主，我想看到同一批有效费用和能量计算的单价，以免分子分母失配。
4. 作为车主，我想从某个结束日日桶看到全部对应会话，以免跨午夜会话丢失。
5. 作为车主，我想组合类型、费用、地点与文本筛选，并稳定翻页。
6. 作为车主，我想按自己的地理围栏或地址核对账目，同名地址仍能区分。
7. 作为车主，我想知道地图坐标来自哪次充电会话，以免误当运营商入口。
8. 作为车主，我想看到原始峰值和有时间覆盖说明的记录平均功率。
9. 作为车主，我想看到记录跨越 20–80 的实际采样端点与质量，以免误认为标准性能测试。
10. 作为车主，我想查看室外温度、电压、电流、续航与原始加热状态，并能分辨不支持和历史缺值。

## Implementation Decisions

### 兼容与范围

- Bearer、只读权限、统一错误、请求 ID、5 秒 SQL 超时继续生效；不读取生产配置，不迁移 TeslaMate，不接触 Tesla 凭据。
- 列表默认 `started_at`，stats 默认 `ended_at`；完成统计始终排除进行中。旧 `cost_per_kwh` 的已发布算法原样保留。
- 响应新增 `query`（`from/to/tz/time_basis/filters`）与 `as_of`；列表省略范围时仍可查询全部历史。stats 保留 730 天上限，地点分组要求显式范围且最多 730 天。显式未来上界裁剪到请求时刻，不生成未来桶。
- 实时 stats/地点响应以弱 ETag 校验除 as_of 外的完整数据；历史数据未变可返回 304。显式未来上界按原始请求边界绑定游标，回显查询时的有效边界，不承诺上游写入期间的快照分页。
- `as_of` 是表示生成时刻；跨端点无原子快照保证。列表/详情/序列继续 30 秒缓存；stats 和地点分组只用 HTTP 条件响应。
- 新能力采用版本标识 `charging-experience-v1`，最低服务端版本 0.2.0。新增源列属于可选能力：缺列/类型不符应声明不支持并返回 null，现有必需列仍按启动探针严格校验；具体兼容基准沿 TeslaMate schema migration `20260808090000`，不宣称已验证全部安装版本。

### CH01：覆盖和均价

- 每个充电桶、totals 与地点组返回：能量/费用/时长有效次数、费用已知且能量非空的入电总量、分类已知次数、未知分类能量及三类记录时长。
- 空匹配集的合计为 0；有会话但费用全未知为 null；能量旧合计保留 0，并通过有效次数区分全缺失。
- `unit_cost_added` / `unit_cost_grid` 对各自费用非空且能量大于 0 的同批会话计算。对象含 value、numerator_cost、denominator_kwh、eligible_sessions、excluded_cost_sessions；无有效分母时 value 为 null。详情用同一规则，进行中不输出最终配对单价。
- `dc_energy_share` 只计算 DC/(AC+DC)，未知不归 AC；不新增效率或损耗指标，不截断 used<added。

### CH02：统一匹配集与分页

- 列表、充电 stats、地点分组共同支持 `time_basis=started_at|ended_at`、`charging_type=ac|dc|unknown`、`location_id`、`query`、`cost_status=known|unknown|zero`，条件取交集。
- 文本搜索折叠首尾/连续空白，最多 100 字符；对围栏名及地址 name/road/house_number/city/neighbourhood 做不区分大小写的字面子串匹配，`%`/`_` 无通配符语义。空白搜索等同省略，不外部逆编码。
- 列表 `sort=started_desc|ended_desc|unit_cost_added_asc|duration_desc`。无效/未知排序值放末尾，同值再按开始时间倒序与 ID 倒序；ended_desc 使用结束时间倒序、开始时间倒序、ID 倒序。
- 新充电游标为带版本的 opaque keyset，绑定车辆、请求时间范围、时区、时间依据、全部筛选与排序；不绑定页大小。非法或条件不一致返回 400 invalid_request。旧行程游标不变；旧充电游标格式在本次增量上线后需重新开始分页。
- 列表返回完整匹配集 `matched_sessions`，每页不返回大聚合。统计使用同一匹配集，跨期会话整体归期，不拆分电量。

### CH03：地点账目

- `GET /v1/vehicles/{vehicle_id}/charging-locations`，完成会话、显式时间范围、默认结束日；每页 1–100 组，返回 total_locations、unlocated_sessions、query、as_of、next_cursor。
- 身份优先会话 geofence_id，其次 address_id，否则统一 unlocated；API 键不透明稳定，客户端不得解析。身份来自当前关联，不代表不变的运营商站点身份。
- 按 `energy_desc|sessions_desc` 排序，同值按 location_id 稳定排序；游标绑定全查询与排序。无坐标组与 unlocated 组仍返回。
- 地点标签及代表点选该组最近一条有完整 position 坐标的会话（开始时间、ID 倒序）；无坐标时标签来自最近会话。标明 representative_session_id 与 coordinate_source=latest_session_position，坐标沿现有 WGS84 原始位置点规则。
- 点地点后用同一键查询列表及 stats，完成匹配集必须一致。

### CH04：原始过程指标

- 详情增加 `process_metrics`；数据库对原始采样计算，原始点不会无界传回应用。
- 有效功率为非空且 ≥0，真实 0 参与；输出原始 power_max_kw 与 power_valid_samples。
- `power_time_weighted_kw` 使用前值保持，且只覆盖会话内相邻采样时间差满足 `0 < dt <= 300s`、前值功率有效的区间。不外推首尾；observed_seconds 为覆盖时长，uncovered_seconds 为会话实际时间跨度减覆盖时长，规则 `previous-hold-v1`。重复时刻区间不计权，下一段按同时间 ID 最后一条作为前值。
- 固定 20–80：会话内按 `(date,id)` 找首次有效 SOC≥20，再找其后首次 SOC≥80；不插值，返回实际 SOC、时间与 `exact|crossed` 阈值精度。
- 状态优先 incomplete_session（不输出最终值）、insufficient_samples（有效 SOC 少于 2）、not_reached（未找到有序阈值）、gap_or_ambiguous（窗口内缺 SOC、重复时间、SOC 回退、相邻间隔 >300 秒）、available。窗口边界前一采样若存在，也参与跨阈值间隔与回退检查。
- 首个下阈值点已达到上阈值时不声称完成窗口；重复时刻不产生伪精确 0 秒，elapsed_seconds 为 null，附原因。规则 `observed-threshold-v1`。

### CH05：扩展采样和原始事实

- 连续字段增加 voltage_v、actual_current_a、pilot_current_a、outside_temp_c、rated_range_km、ideal_range_km、charge_energy_added_kwh；字段共用采样索引，各字段未请求省略，请求无值为 null。
- 继续 SQL ≤4×points + LTTB ≤points、首尾保留；第一请求字段用于 SQL 桶极值，多字段 LTTB 不保证每个原始峰值保留。累计能量可能重置，不解释为区间增量。
- 加热四个原始 boolean 独立保留。请求加热字段时返回独立 `state_events`：每字段首状态与每次变化（包括 null）按时间/ID排序，最多 1000 个事件，额外计数并用 truncated 标明不完整。事件不参与连续曲线插值。
- 详情增加原始 rated/ideal 起止续航及最后采样的 fast_charger_present/brand/type、cable、source_sample_at；标明 latest_charge_sample 来源，不从地点名称猜测，不覆盖 is_dc。

## Testing Decisions

- 沿既有、已由维护者批准的唯一业务 HTTP/ASGI 缝，验证真实隔离 PostgreSQL 返回；不 mock SQL，不新增内部测试缝。
- 按垂直切片逐个 red → green；覆盖提案的固定合成例、缺失与 0、错配分母、跨午夜/DST/上界、筛选翻页、同名地点、原始峰值、非均匀采样/长缺口/回退/重复时刻、缺列能力和 Bearer/404/ETag。
- 新建本地夹具库，不继承生产 DSN；每个切片运行相关测试及 mypy，最终运行完整 pytest、ruff 和双轴 code-review。

## Out of Scope

CH06、地图簇、运营商目录、费用写入/汇率/分时电价、生产数据库访问/装载、部署/推送、关闭 #28 或宣称已完成 Grafana 对账。

## Further Notes

- 这是 #4/#5 的增量，不把旧 spec 或已完成实现重新标记为未完成，也不关闭父票。
- 实施票状态依次 Ready → In progress → Implemented locally；测试和审查证据回链，未推送的提交不冒充已合并代码。

## 验证与交付记录（2026-10-28）

- 维护者确认范围及测试缝后发布 spec #29 和实施 #30–#33，再开始实现；旧 spec #4/#5 用评论记录增量关系，不覆写原口径。
- 当前分支 `codex/b-wave-implementation`；实现提交 `5b4ddf9`，后续审查修复与本记录一同提交。
- 最终完整回归：`UV_CACHE_DIR=.cache/uv uv run pytest` → **165 passed, 8 deselected**。
- 批量分类修复后的充电/分类/统计回归 64 项通过；新增大夹具 slow 单独通过（5000 会话、100000 采样，三个充电查询均未超 5 秒）。未运行其余既有 slow 用例。
- `ruff check src tests` 与 `mypy` 通过。
- Standards 初审 1 项：地点 OpenAPI 必填范围/默认排序不符；已补端点声明及 HTTP 断言，复核通过。
- Spec 初审 2 项：序列嵌套元数据被省略、极端时间游标返回 500；均先复现红灯，再修复并验证，复核通过。两轴无遗留发现。
- 冻结文件：`docs/api/openapi-0.2.0.json`，SHA-256 `94cab04beb035fbf0b7423b61965138be734034630e91477267fef1f8a7d41ea`；HTTP 测试校验与运行时 OpenAPI 一致。
- 新增实施票按本地实现完成回填并关闭；父 spec #29 保留开放。#28 真实 Grafana 验收继续独立开放，未推送/部署，不将夹具测试冒充生产对账。
