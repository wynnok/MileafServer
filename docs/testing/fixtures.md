# TeslaMate 夹具基准与场景

A2（#8）提供只读 API 的 PostgreSQL 测试数据；业务口径由后续 C/D 切片实现，本文件只描述夹具。所有数据都是合成数据，不导出真实实例，也不存储 Tesla 凭据。

## 来源与版本

- 上游：[TeslaMate v4.3.0](https://github.com/teslamate-org/teslamate/tree/v4.3.0)。本地 `teslamate` 符号链接仅用于实施时核对；运行测试不需要它。
- tag 对象：`cf1380d3c1a57dc8ee3c204366ea2dd8e3e1902d`；实际 commit：`33d200b2fba9d5138803916a788cef5eae31b1aa`。
- 该 tag 有 105 条 migration，完整版本号在 `tests/fixtures/migration_versions.txt`，最大值 `20260808090000`。B1 的 `tested` 已验证区间只能将该最大版本作为单点基准，不能据此声称整段版本都经过测试。
- 本地 checkout 的 `VERSION` 是 `4.4.0-dev`（HEAD `6bf3d55e4242cf9c55d1dd334b18e35c9ff59d9f`），不作为本次夹具基准。该版本额外有 `20261009120000` 和 `20261014120000` migration；后者把 `range` 类型改名为 `preferred_range`。

`tests/fixtures/schema.sql` 是随仓库提交的 API 表投影 SQL，由上述 tag 的 Ecto schema 与 migration 独立转写。它覆盖 `settings`、`car_settings`、`cars`、`states`、`positions`、`drives`、`charging_processes`、`charges`、`geofences`、`addresses`、`updates` 及版本表。类型、数值精度、主要空值与外键/检查约束依照上游；不包含导入任务表、地理扩展/函数和查询性能索引，不是可用于恢复 TeslaMate 的完整备份。`schema_migrations` 的 105 行是来源版本标记，不表示测试运行了上游 migration。

核对位置均在该 tag 的 `elixir/` 下：

| 内容 | 上游来源 |
|---|---|
| 领域字段 | `lib/teslamate/log/{car,state,position,drive,charging_process,charge,update}.ex`、`locations/{address,geo_fence}.ex`、`settings/{global_settings,car_settings}.ex` |
| 表基础与空值 | `priv/repo/migrations/20190330*_create_*.exs`、`20190415130006_create_addresses.exs`、`20191117171307_car_settings.exs` |
| numeric 精度与小整数 | `20200410112005_database_efficiency_improvements.exs`、`20260718160000_increase_geofence_cost_precision.exs` |
| UTC 无时区时间戳 | `20191020130234_increase_datetime_precision.exs`（Ecto `utc_datetime_usec` 对应 `timestamp(6) without time zone`） |
| 私有凭据表位置 | `20250924215353_create_private_schema.exs`（投影只有空表，用于权限探针） |
| 正相数约束 | `20260807090000_enforce_positive_charger_phases.exs` |
| 展示单位枚举名 | `20200528173223_rename_unit_enums.exs` |

## 执行与隔离

先在测试之外准备镜像，然后从项目根目录运行：

```sh
docker pull postgres:16-alpine
UV_CACHE_DIR=.cache/uv uv run pytest tests/test_fixtures.py
```

测试使用预拉取的 `postgres:16-alpine`；启动参数固定 `--pull=never`，不会自动拉镜像，也不运行需要联网下载 sidecar 的容器清理器。session 启一个 PostgreSQL 容器，仅向 `127.0.0.1` 发布随机端口；每个 function 创建独立数据库，结束时删除，session 结束停止容器。容器只含合成数据，使用本机回环端口与 PostgreSQL trust 认证。`admin_dsn` 是夹具管理员连接，`db` 用它加载 schema 与种子；`database_dsn` 使用独立的非 owner 只读角色，仅可读取 public schema 的表，不能读取 `private.tokens`。应用仍通过自身连接层设置只读事务。无 Docker 或无镜像时，只有依赖 PostgreSQL 的测试跳过，纯单元测试照常运行。

每个域的 `seed(db)` 可以在依赖域已加载后独立重放：车辆与状态 → 围栏与地址 → 行程与位置 → 充电 → 固件。显式主键与 `ON CONFLICT DO NOTHING` 保证对未修改的基础数据重复加载不增行；重放不承担修复测试主动改坏的数据的职责。每条测试由新数据库恢复基础数据。

需要局部特殊车辆时使用 `tests.fixtures.seeds.vehicles.seed_car(db, efficiency=None, unit_of_length="mi")`；它增加车辆并设置站点的展示单位，不改变里程或速度的存储单位，也不修改基础种子文件。该测试结束即丢弃整个数据库。

`health_client` 通过真实应用 lifespan 与 HTTPX ASGI transport 访问夹具数据库，依赖覆盖 `get_now` 固定为 `2026-03-15T12:00:00Z`。`client` 通过真实 setup/login 获取 token 并自动携带 Bearer 头，不伪造 token 或绕过认证。

## 性能冒烟（#27）

默认 `uv run pytest` 排除 `slow`，不生成压测数据。显式运行：

```sh
UV_CACHE_DIR=.cache/uv uv run pytest -m slow
```

`performance_seed` 仅允许带 `slow` 标记的测试使用，并延迟导入生成器。
固定随机种子为 `27`，专用车辆 `cars.id=3` 下有 20,000 条行程、200,000 个位置点、
5,000 次已结束的充电会话，每次 20 个采样（总计 100,000 个充电采样，AC/DC 各半）。
这些是基础场景以外的数据，时间覆盖 2025-04-01 至固定时钟前；其中一条行程含
20,000 个位置点，用于验证 `points=200` 的降采样路径。生成器使用保留主键，
在新数据库或事务回滚后重新加载可重现相同记录；不会覆盖基础场景，不能在已加载的库中重复追加。

性能测试复用上述容器及每条测试独立的数据库，清理由原有夹具完成。
测量使用真实鉴权 HTTP API、HTTPX ASGI transport 与 PostgreSQL；不包含外部网络、
容器启动、数据生成或 setup/login 的耗时。关闭应用缓存，数据库页缓存和预编译语句可复用。
这些预算是单机回归信号，不是生产环境的绝对 SLO。

| 端点 | 参数 / 采样 | 预算 |
|---|---|---|
| 行程列表 | `limit=20`，20 次请求，nearest-rank p95（排序后第 19 个） | ≤ 300 ms |
| 行程序列 | 密集行程，`points=200`，3 次请求的最大值 | < 600 ms |
| 统计 | `kind=driving&bucket=month`，2025-04 至 2026-04，3 次请求的最大值 | ≤ 800 ms |
| 概览 | 3 次请求的最大值 | ≤ 400 ms |

计时包含第一次请求，不额外预热。每项均校验 HTTP 200 和非空有效响应，
并在默认 pytest 输出捕获下打印端点、参数、实测值、预算与差额（`delta=实测−预算`）。
超预算时，断言消息也包含上述诊断。统计冒烟覆盖驾驶统计；充电统计不在本次计时范围内。
夹具沿用基础投影的索引，不额外建索引，也不修改任何运行时查询。
另有规模及回滚后重建一致性检查，以及通过运行时连接池使用 owner 角色执行写入的测试：
必须得到 `ReadOnlySQLTransactionError`，包括连接归还后复用，避免仅凭角色权限拒绝掩盖事务保护失效。

## 场景与 SQL 定位

以下每项由 `tests/test_fixtures.py` 的 SQL 或真实 `/v1/health` 请求验证。

| 种子用例 | 稳定定位 / 用途 |
|---|---|
| 命名车辆、空车辆 | `cars.id=1/2`；2 号无位置点与行程、名称与效率为 NULL，测试空状态 |
| 默认展示设置 | `settings.id=1`，`preferred_range='rated'`、`unit_of_length='km'` |
| 在线/休眠/离线/在线 | `states.id=1..4`；休眠区间内无位置点，不插值 |
| 最新完整位置点 | `positions.id=999999`，3/15 11:55 UTC，电量 68、额定续航 320 km、估算续航 300 km、海拔 42 m、内外温 22/16°C、GPS |
| 今天已完成两条、昨天、本月、上月 | `drives.id=1/2,3,4,5`；固定时钟窗口 |
| 跨午夜 | `drives.id=6`，3/14 23:30 → 3/15 00:40 UTC；UTC/上海窗口差异 |
| 进行中 | `drives.id=7`，结束时间 NULL |
| 续航差非正、零距离 | `drives.id=8/9` |
| 围栏/地址/坐标/全缺 | `drives.id=10/11/12/13`，起终点各覆盖同类回退 |
| 连续/稀疏/缺失序列 | `drive_id=1/2/13` 的位置点分别为 121（严格一分钟）、3、0 |
| 柏林 DST 行程 | `drives.id=14..17`，3/28 22:30、3/29 00:30、3/29 21:30、3/30 02:00 UTC；只留行程头，不生成未来位置点污染 freshness |
| 稳定分页数据 | `drives.id=18..50`；总计 50 条，无随机数 |
| 家充 AC / 超充 DC | `charging_processes.id=1/2`，各 61 点；AC 相数 3，DC `fast_charger_present=true`、相数 NULL |
| 费用未知/明确免费 | `charging_processes.id=3/4`，费用分别 NULL/0 |
| 无采样/两个采样 | `charging_processes.id=5/6` |
| 判定信息不完整 | `charging_processes.id=7`；相数含 NULL，非空相数 1/3 数量相同，`fast_charger_present` 全 NULL |
| 进行中充电 | `charging_processes.id=8`，结束时间 NULL，最后采样 3/15 11:50 UTC |
| DST 充电 | `charging_processes.id=9`，3/29 01:00 UTC；分桶边界 |
| 家/公司围栏 | `geofences.id=1/2` |
| 街道门牌/仅城市地址 | `addresses.id=1/2` |
| 固件记录 | `updates.id=1`，版本 2026.2.3，2/15 08:30 安装结束 |
| 连通性 | `health_client.get('/v1/health')`，真实 PostgreSQL |

#6 的主数据截止 3/15 与明确列出的 3/28–30 DST 场景冲突：保留显式 DST 特例，其余主数据从 1/1 到固定时钟，时钟不随运行时间改变。未来 DST 行程与会话用于分桶边界；它们也晚于固定时钟，因此概览的 `last_drive` / `last_charge` 可能选中这些记录，而 `freshness` 仍只取时钟前的位置点/状态。

另一个已核实并经用户确认的偏差：v4.3.0 migration 已将非正 `charger_phases` 清为 NULL，且有 `CHECK (charger_phases > 0)`。因此不能照 #6 装载相数为 0 的 DC 数据；保留真实约束并改用 `fast_charger_present=true` 的超充采样。B8（#17）已按此修正 AC/DC 判定，并保留上游约束；不能通过删除约束实现旧假设。
