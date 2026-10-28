# Mileaf API v1 接口面

契约先于实现：本文件是 `/to-spec` 的输入，实现为 FastAPI（见 ADR-0001），但契约本身与语言无关。OpenAPI 3.1 是最终事实来源，客户端由它生成 Dart 代码。

## 设计原则

- 只读资源导向：除认证外全部 `GET`。
- 一切查询都带时间范围，默认值明确，不做「全表返回」。
- 服务端只做聚合、裁剪、降采样，不重算 TeslaMate 已经算好的指标（见 ADR-0002）。例外：电耗与能量不是存储列，必须按 TeslaMate 的既有口径推导——`energy_kwh = (start_range_km − end_range_km) × cars.efficiency`，`cars.efficiency` 单位是 kWh/km，取哪个 range 由 `settings.preferred_range` 决定；`range_diff ≤ 0` 或效率缺失时返回 `null`，不返回负值。
- 概览、车辆列表与元信息自带 `freshness`；其它 DTO 以自身时间字段表达数据时间，不额外伪造新鲜度。
- 不向客户端暴露 TeslaMate 表名；DTO 字段用 `GLOSSARY.md` 的代码名。

## 通用约定

| 项 | 约定 |
|---|---|
| Base path | `/v1` |
| 认证 | `Authorization: Bearer <token>`；仅 `/v1/health` 与 `/v1/auth/*` 匿名 |
| 内容类型 | `application/json; charset=utf-8` |
| 字段命名 | `snake_case`（OpenAPI 生成器负责映射 Dart 命名） |
| 时间 | ISO-8601 UTC，带 `Z`；区间为半开 `[from, to)` |
| 距离 / 能量 / 功率 / 温度 | km / kWh / kW / °C |
| 电耗 | Wh/km；行程级只有网侧单值 `consumption_wh_km`；统计级区分 `consumption_wh_km_net` 与 `consumption_wh_km_gross` |
| 时长 | 分钟（整数），字段名 `duration_min` |
| 分页 | `limit`（默认 20，最大 100）+ `cursor`（不透明串）；非法参数返回 400；响应含 `next_cursor`，`null` 表示到底 |
| 排序 | 默认按 `started_at` 倒序，同时间按 `id` 倒序 |
| 可空 | TeslaMate 尚未回填的聚合值返回 `null`，不用 0 代替 |
| 缓存 | 列表、详情、统计与概览返回 `ETag` 和 `Cache-Control: private, max-age=30`；内容未变返回 304；概览、行程与充电端点另有进程内 30 秒 TTL 缓存，统计实时聚合、不做服务端缓存 |
| 错误 | `{"error": {"code": "...", "message": "..."}}`，`code` 稳定，`message` 可读 |
| 版本 | `/v1` 内只做加法；破坏性变更开 `/v2` |
| 服务端超时 | 单查询 `statement_timeout` 5s，超时返回 `503 upstream_timeout` |

`freshness` 结构（出现在概览、车辆列表、元信息中）：

```json
{"last_position_at": "2026-10-21T12:30:00Z", "last_state_at": "2026-10-21T12:29:00Z"}
```

## 端点

### 认证与元信息

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/auth/setup` | 首次初始化，设置访问口令；已初始化返回 `409 already_initialized` |
| POST | `/v1/auth/login` | 口令换 Bearer token；返回 `token`、`expires_at` |
| GET | `/v1/health` | 匿名；`{"status":"ok","version":"..."}` |
| GET | `/v1/meta` | 服务版本、TeslaMate migration 版本、`schema_ok`、`tested`、单位制固定 `metric`、`source_unit_of_length`、freshness |

token 使用 32 字节密码学随机数的 base64url 编码，TTL 为 30 天，不刷新；过期返回 401。最多保留 10 个 token，超出时淘汰创建时间最早的 token。登录失败按 IP 限制为每分钟 5 次，超出返回 429。状态文件仅保存口令哈希、token 摘要及创建和过期时间，不保存明文 token（见 ADR-0003）。

setup/login 的 JSON 请求体为 `{"password":"YOUR_ACCESS_PASSWORD"}`。setup 成功返回
`{"status":"ok"}`；不足 10 字符返回 `400 invalid_request`。缺失、错误或过期 Bearer
及错误口令统一返回 `401 unauthorized`，限流返回 `429 rate_limited`。

### 缓存原语接入

业务端点通过 `get_cache` 获取应用级 `TTLCache`，给 `cached_response` 传入完整缓存键、
生成 JSON **字节**的异步 `render` 和端点 TTL。键必须含资源类型、车辆、时区、时间范围、
分页及其他影响响应的参数；`freshness` 在生成响应时取数据时间，命中时复用原字节。
TTL 使用单调时钟，默认 30 秒；容量默认 256 条，满时淘汰最久未使用的键。
`MILEAF_CACHE_TTL_SECONDS=0` 关闭进程内缓存，不改变序列化和 ETag 算法。
命中/未命中通过缓存对象的 `hits`/`misses` 计数观察，不放进响应体。

ETag 哈希覆盖已序列化 JSON、路径与完整查询串；支持 `If-None-Match` 的列表、弱比较
和通配符。304 不带响应体，保留 ETag 与 `Cache-Control: private, max-age=<ttl>`。
统计等只需 HTTP 条件响应、无需进程内缓存的端点直接调用 `conditional_response`。
各业务端点已按上述约定接入；统计只使用条件响应，不复用进程内缓存。

### 车辆

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/vehicles` | 车辆列表，含当前状态、电量、续航、freshness |
| GET | `/v1/vehicles/{id}/summary?tz=<IANA>` | 首页概览聚合；`tz` 默认 `UTC`，非法返回 400；今天与本月按该时区本地日历切分 |

### 行程

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/vehicles/{id}/drives?from&to&tz&distance_min_km&distance_max_km&outside_temp_min_c&outside_temp_max_c&energy_status&completed_only&cursor&limit` | 行程分页列表；筛选与匹配回显见 0.3.0 增量 |
| GET | `/v1/drives/{id}` | 行程详情（含起终点地址与围栏、温度、速度极值） |
| GET | `/v1/drives/{id}/series?fields=speed,battery,elevation&points=200` | 图表序列，服务端降采样 |

### 充电

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/vehicles/{id}/charging-sessions?from&to&cursor&limit` | 充电会话分页列表 |
| GET | `/v1/vehicles/{id}/charging-locations?from&to&cursor&limit` | 充电地点账目分页（0.2.0 增量） |
| GET | `/v1/charging-sessions/{id}` | 会话详情（能量、费用、AC/DC、温度） |
| GET | `/v1/charging-sessions/{id}/series?fields=power,soc&points=200` | 功率/电量曲线 |

### 统计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/vehicles/{id}/stats?kind=driving\|charging&bucket=day\|week\|month\|year&from&to&tz=<IANA>` | 按桶聚合；`tz` 默认 `UTC`，非法返回 400 |

延后屏幕的端点先占位、不实现：`/states`、`/timeline`、`/battery-health`、`/updates`、`/locations`。

## 分页与序列

行程列表游标为 `base64url("<started_at>|<id>")`，客户端不得解析；非法编码或内容返回 400。按 `(started_at, id)` 倒序，以严格小于游标键的记录继续翻页。`limit` 必须为 1–100，默认 20，超出范围返回 400。

行程与充电会话列表响应为 `{"items": [...], "next_cursor": null}`。`from` / `to`
均可省略，省略的一侧不限制时间；提供时必须带时区，且同时提供时必须满足 `from < to`。
列表默认按开始时间筛选，充电增量支持结束日与额外筛选，见末节；进行中的记录也可返回；其结束时间、最终时长、最终电量为 null，
进行中行程的距离、能量、电耗也为 null，充电会话的能量保留当前累计值。

两种序列端点共用两段式降采样：SQL 按时间桶先压到不超过 `4 × points` 行，应用内再用 LTTB 压到不超过 `points` 个点，两段都必须保留原始首尾点。`points` 默认 200、范围 2–1000，超出范围返回 400；最小值为 2，确保多点序列能同时保留首尾。`fields` 按端点白名单校验，未知字段返回 400。原始点数不足时原样返回，不放大。结果按 `t` 升序，采样缺失的字段为 `null`，时间空洞不插值。行程序列始终返回 `lat`/`lon`。

`resolution` 只有两种值：有采样点时为 `server_sampled`，表示序列经服务端采样管线处理，点数少时也可能原样返回；无采样点时为 `empty`，此时 `series: []`，HTTP 200。客户端不得据此推断原始点数。

行程 `fields` 默认 `speed`，可选 `speed,battery,elevation`，点字段分别为
`speed_kmh`、`battery_level`、`elevation_m`；每点始终含 `t`、`lat`、`lon`。
充电会话 `fields` 默认 `power,soc`，点字段同名，单位为 kW 和百分比。
未请求的可选点字段不返回，请求的缺失值返回 null。序列顶层含 `drive_id` 或
`session_id`、`resolution`、`points`（实际返回点数）与 `series`。

充电会话详情的 `energy_added_kwh` 是电池充入能量，`energy_used_kwh` 是电网侧消耗，
两者均保留上游 null；不返回币种。`charger_phases_mode` 是非 null 相数的众数，
无有效相数时为 null；`is_dc` 复用上述 AC/DC 口径。

## 统计桶与时间窗口

统计 `from`/`to` 必填，必须满足 `from < to`，半开区间 `[from, to)` 上限为 730 天；非法或超长区间返回 400。按请求的 IANA 时区用 `date_trunc(bucket, started_at AT TIME ZONE tz) AT TIME ZONE tz` 得到桶起点，周从周一开始。桶边界按本地日历推进，再转回 UTC；夏令时切换日覆盖 23 或 25 小时，不按固定 24 小时截桶。概览的今天与本月使用同一时区规则。

驾驶统计按行程 `started_at` 归桶，充电统计按充电会话 `ended_at` 归桶；只计已结束记录，首尾桶仅统计请求区间内的数据。返回 `buckets` 与 `totals`，空桶补零、无数据时比率为 `null`；总计合并分子分母后计算比率，不平均各桶比率。每种 `kind` 用一条聚合查询完成，不按桶循环查询。

## 单位归一化与口径来源

API 一律返回 km、km/h、kWh、kW、°C、Wh/km。`settings.unit_of_length` 可为 `km` 或 `mi`，是 TeslaMate 的展示偏好，**不是数据库距离与速度列的存储单位**；`/v1/meta.source_unit_of_length` 暴露该设置，非法值拒绝启动。客户端需要英制时自行从 API 公制值换算。

TeslaMate 在写入位置点前就用 `0.62137119223733` 将 Tesla 原始英里与 mph 分别除以该因子，转换为 km 与 km/h。`positions.odometer`、`positions.speed`、由 odometer 首尾差分得到的 `drives.distance`、`drives.speed_max` 均已为公制，不得根据 `unit_of_length=mi` 再除一次。`*_range_km` 恒为 km，`cars.efficiency` 为 kWh/km，温度恒为 °C，同样不重复换算。API 展示精度为距离 1 位小数、速度与电耗取整；聚合使用未舍入值。

来源核验（TeslaMate `v4.3.0`）：[`vehicle.ex` L1754–1760](https://github.com/teslamate-org/teslamate/blob/v4.3.0/elixir/lib/teslamate/vehicles/vehicle.ex#L1754-L1760) 在采集时换算，流式采样在 L1794–1797 同样换算；[`log.ex` L303–305](https://github.com/teslamate-org/teslamate/blob/v4.3.0/elixir/lib/teslamate/log.ex#L303-L305) 从公里 odometer 差分行程距离；[`convert.ex`](https://github.com/teslamate-org/teslamate/blob/v4.3.0/elixir/lib/teslamate/convert.ex) 定义换算因子。本地 `4.4.0-dev`（`6bf3d55e4242cf9c55d1dd334b18e35c9ff59d9f`）亦如此。

工单差异（2026-10-22 维护者确认）：#6、#9、#18 中「距离与速度随 settings 变单位」的假设与上述源码冲突，以源码核实结果为准；B9 的单位入口应保留数据库公制值，英制展示偏好不得触发二次换算。ADR-0002 的「不重算」指不发明新指标口径，必要的真实源单位换算不属于重算。

凡复刻 TeslaMate dashboard 口径的 SQL，必须在 SQL 注释中写明来源面板、公式与所用单位，按语义重写，不直接复制上游 SQL/JSON：

| 口径 | 来源面板与必须注明的规则 |
|---|---|
| 行程能量与净电耗 | `drives`：range 差 × efficiency 得到 kWh，再除公制距离 × 1000 得到 Wh/km；注明 preferred range、非正 range 差与缺失效率的 null 规则 |
| 地点显示名 | `drives`：围栏 → 地址名 → road + house_number → 城市/街区 → null；坐标另行保留 |
| AC/DC 判定 | `charging-stats`：注明采用的 phases 众数公式、空值规则与适用版本；不得静默把未知值当 AC 或 DC |
| 毛电耗 | `overview`：区间内充入能量之和 ÷ 行驶里程 × 1000；充电按结束时间、行程按开始时间归期，缺少充入能量或里程为零时为 null |

## TeslaMate 兼容基准与升级

夹具与当前声明的验证基准为已发布的 TeslaMate `v4.3.0`，commit `33d200b2fba9d5138803916a788cef5eae31b1aa`（annotated tag 对象为 `cf1380d3c1a57dc8ee3c204366ea2dd8e3e1902d`），共 105 个 migration，最大版本 `20260808090000`。已知 `tested` 区间当前是单点闭区间 `[20260808090000, 20260808090000]`，不据此宣称验证了中间版本或本地 `4.4.0-dev`。夹具 schema 是 API 所需表的投影，不是完整数据库导出。

`/v1/meta.tested` 仅在 `schema_migrations` 最大版本落在已知区间内且必需结构完整时为 `true`；结构完整但版本在区间外时为 `false`，服务仍启动。缺少必需表/列时拒绝启动并说明缺项，不返回伪造空数据；因此正常提供 meta 时 `schema_ok` 为 `true`。凭据表读取权限探针必须针对该版本实际所在 schema（本基准为 `private.tokens`）检查权限，不读取凭据内容。

schema 快探针清单随实现切片扩充：骨架切片需 `cars`、`states`、`positions`、`schema_migrations`；元信息与业务切片再校验 `settings`、`drives`、`charging_processes`、`charges`、`addresses`、`geofences`、`updates` 及实际查询引用的列和类型。所需列的真实结构以同基准的夹具 schema 为参照，权限检查独立于业务表投影。

升级支持基准时，必须先核对上游 migration、写入时的单位与 dashboard 口径，再同步更新夹具 schema/种子、schema 快探针的必需表列与权限检查、`tested` 区间及本节版本记录；通过共享原语与 HTTP 契约回归、同区间 Grafana 对账后再扩大验证范围。仅版本号更高不能直接标为 `tested=true`。

已知工单差异：本基准已要求 `charger_phases > 0`（允许 NULL），DC 采样使用 `fast_charger_present=true` 且 phases 为 NULL。#4、#6、#17 的「phases 众数为 0 即 DC」假设不适用于本基准；维护者已确认保留真实 schema 与 DC 数据形状，B8 实施时应相应修正判定并注明来源，不为满足旧假设移除上游约束。

## 关键响应形状

概览（`/v1/vehicles/{id}/summary`）：

```json
{
  "vehicle": {"id": 1, "name": "Model 3", "model": "3", "vin_suffix": "4821"},
  "state": {"value": "online", "since": "2026-10-21T12:00:00Z"},
  "battery": {"level": 78, "range_km": 412, "range_kind": "rated"},
  "odometer_km": 42123,
  "charging_power_kw": null,
  "period": {
    "today": {"drives": 2, "distance_km": 34.2, "energy_kwh": 5.6},
    "month": {"drives": 18, "distance_km": 412.3, "energy_kwh": 68.1}
  },
  "last_drive": {"id": 9021, "started_at": "2026-10-21T08:12:00Z", "distance_km": 12.3, "consumption_wh_km": 168},
  "last_charge": {"id": 331, "started_at": "2026-10-20T22:04:00Z", "energy_kwh": 31.2, "end_soc": 80},
  "firmware": {"version": "2026.32.6", "installed_at": "2026-10-02T03:11:00Z"},
  "freshness": {"last_position_at": "2026-10-21T12:30:00Z", "last_state_at": "2026-10-21T12:29:00Z"}
}
```

行程详情（`/v1/drives/{id}`）：

```json
{
  "id": 9021,
  "vehicle_id": 1,
  "started_at": "2026-10-21T08:12:00Z",
  "ended_at": "2026-10-21T08:33:00Z",
  "distance_km": 12.3,
  "duration_min": 21,
  "energy_kwh": 2.1,
  "consumption_wh_km": 168,
  "speed_max_kmh": 112,
  "outside_temp_c_avg": 18.2,
  "inside_temp_c_avg": 21.0,
  "start": {"address": "上海市…", "geofence": "家", "lat": 31.2304, "lon": 121.4737},
  "end": {"address": "上海市…", "geofence": null, "lat": 31.2152, "lon": 121.4401}
}
```

序列（`/v1/drives/{id}/series`）：

```json
{
  "drive_id": 9021,
  "resolution": "server_sampled",
  "points": 200,
  "series": [{"t": "2026-10-21T08:12:00Z", "lat": 31.2304, "lon": 121.4737, "speed_kmh": 0, "battery_level": 78, "elevation_m": 8}]
}
```

## 屏幕 ↔ 端点 ↔ 数据源

| 屏幕 | 端点 | TeslaMate 表 |
|---|---|---|
| 车辆切换 | `/vehicles` | `cars`、`states`、`positions` |
| 首页概览 | `/vehicles/{id}/summary` | `cars`、`states`、`positions`、`drives`、`charging_processes`、`updates` |
| 行程列表 | `/vehicles/{id}/drives` | `drives`、`addresses`、`geofences` |
| 行程详情 | `/drives/{id}` + `/series` | `drives`、`positions`、`addresses`、`geofences` |
| 充电列表 | `/vehicles/{id}/charging-sessions` | `charging_processes`、`addresses`、`geofences` |
| 充电详情 | `/charging-sessions/{id}` + `/series` | `charging_processes`、`charges` |
| 统计 | `/vehicles/{id}/stats` | `drives`、`charging_processes` |

## 性能预算

| 端点 | p95 目标 | 备注 |
|---|---|---|
| `/vehicles` | 200ms | 表极小 |
| `/summary` | 400ms | 多条子查询，进程内 30s TTL 缓存 |
| 列表类 | 300ms | 强制分页 + 覆盖索引 |
| 详情类 | 500ms | 单行 + 关联 |
| `/series` | 600ms | 默认 ≤ 200 点，最大 1000；SQL 时间桶 + 应用内 LTTB |

## 分页、序列与充电分类原语接入

这些原语只提供查询能力，业务端点在各自切片接入。调用方从连接池获取连接；
所有 SQL 表名、表达式、过滤条件和输出列名必须是开发者常量，请求值通过 `args`
绑定，不能拼进 SQL。调用方为实际使用的业务列注册 schema capability。
原语查询在只读事务内设置局部 `statement_timeout=5s`，同时设置驱动超时；
超时抛出 `APIError(503, upstream_timeout)`，事务结束恢复连接设置。

- `mileaf.queries.pagination.fetch_page` 接收输出 `id` 与 UTC `started_at` 的
  `source` SELECT、其 `args`，以及 `limit`、`cursor`、可选的 `from_`/`to`。
  TeslaMate 的无时区列须用 `start_date AT TIME ZONE 'UTC'` 转换。
  返回 `Page(rows, next_cursor)`；原语负责多取一行判断是否到底并编码游标。
- `mileaf.queries.series.fetch_series` 接收 `table`、`time_column`、字段白名单
  `columns`（输出名到数值 SQL 表达式的映射）、`where` 与 `args`，以及
  `fields`、`points`。源表须有唯一 `id`；时间列为 TeslaMate 的 UTC 无时区列。
  `fields` 缺省选择映射中的所有字段，端点负责传入其约定的默认值。
  行程端点传 `required_fields=("lat", "lon")` 保证坐标始终存在。
  SQL 每个时间桶保留首尾及首个请求字段的最小/最大原始点，总量不超过
  `4 × points`；随后 LTTB 对各字段归一化后的三角面积求和选点，返回
  `Series(series, resolution)`。空值不贡献面积，输出仍保留原始 NULL。
  该方法保留首尾与曲线形状，不保证每个字段的所有局部极值都被保留。
- `mileaf.queries.charging.is_dc_sql(session="cp")` 可嵌入列表或分桶查询；
  `charging_energy` 按车辆和可选 UTC `[from_, to)` 结束时间区间，一次聚合
  已完成会话，返回 `ac_energy`、`dc_energy`、`unknown_energy`、`total_energy`。
  能量统一取 `charge_energy_added`（kWh，与 #5 的充入能量一致），不取面板
  electricity usage 的 `greatest(added, used)`。未知分类不并入 AC/DC。
  分类沿用 v4.3.0 charging-stats 的非空相数众数；全 NULL 为 DC，完全无采样
  为未知。`fast_charger_present` 不覆盖该面板规则；相数众数并列时 PostgreSQL
  按升序取首个值，在当前正相数约束下不影响 AC/DC 分类。

地点通过 `mileaf.queries.locations.location_sql(position="p", address="a", geofence="g")`
在已关联位置点、地址、围栏的 SELECT 中输出结构化 JSONB；别名只能来自开发者常量。
`fetch_locations(connection, refs)` 可将一页行程的起终点或充电会话的
`LocationRef(position_id, address_id, geofence_id)` 合并为一次查询，并按输入顺序
返回 `Location`，重复引用不丢失。`address` 为地址自身的回退显示名，`geofence`
为围栏名；`display_name` 优先围栏。空白名称按缺失处理，坐标只来自位置点。

### 驾驶与充电统计（C4、D1、D2）

`driving` 固定键：`drives`、`distance_km`、`energy_kwh`、`consumption_wh_km_net`、
`consumption_wh_km_gross`、`speed_max_kmh`。`charging` 原有核心键：`sessions`、
`energy_added_kwh`、`cost`、`cost_per_kwh`、`ac_energy_kwh`、`dc_energy_kwh`、
`dc_energy_share`。桶除这些键外含 UTC `bucket_start`；totals 不含时间键。

驾驶按已结束行程的 `start_date` 归桶，`drives` 与 `distance_km` 包含所有已结束行程。
`energy_kwh` 逐行程复用 preferred range 差 × efficiency 的推导后求和，跳过未知值；
因此 range 差非正或效率缺失的行程仍计入次数与里程，但不贡献能量，净电耗的分母仍是全部里程。
非空桶中全部行程的能量未知时，`energy_kwh` 与 `consumption_wh_km_net` 均为 `null`；
没有行程时能量为 0。净电耗为 `sum(energy_kwh) / sum(distance_km) × 1000`，
里程为零时为 `null`。`speed_max_kmh` 为各行程最高速度的最大值，无已知速度时为 `null`。

毛电耗沿用 overview：已结束充电会话的 `charge_energy_added` 按 `end_date` 归桶，
除以同桶驾驶里程再乘 1000。充电与行程的归期不同，必须保留 `_gross` 标识；
无充电能量（包括合计为零）或无里程时为 `null`。仅有充电的桶仍纳入区间毛电耗的分子。

充电默认按已结束会话的 `end_date` 归桶（增量可选开始日），`energy_added_kwh` 合计 `charge_energy_added`。
`cost` 只合计已知费用；明确免费（0）参与合计，非空桶中全部费用未知时为 `null`。
`cost_per_kwh` 为已知费用合计除以**费用已知会话的能量合计**，不把未知费用会话的能量
放进分母；分母为零时为 `null`。费用与单价不做币种换算。
AC/DC 分类复用上面的 phases 众数规则；未知分类只计入次数与总能量。
`dc_energy_share` 为 `dc_energy_kwh / (ac_energy_kwh + dc_energy_kwh)`，分母为零时为 `null`。
所有计数、里程与能量、费用在空桶和空区间总计中补 0，比率与最高速度为 `null`。

`GROUPING SETS` 在同一查询内计算各桶与区间总计；
`mileaf.stats.ratio_sql(numerator=..., denominator=...)` 对可加分子、分母先求和再相除，
分母为零或缺失时为 null，禁止对桶比率求平均；最高速度合并取最大值。
距离、速度与电耗仅在响应前按公制精度舍入，聚合与比率均使用未舍入值。
统计每次从数据库聚合，仅使用 HTTP 条件响应，固定 `private, max-age=30`。

固定夹具的三个月驾驶净电耗手算（`rated`、efficiency = 0.15 kWh/km）：

| 月份 | 已结束行程数 | 里程 km | 能量 kWh | 净电耗 Wh/km（展示取整） |
|---|---:|---:|---:|---:|
| 2026-01 | 31 | 620 | `31 × (350−325) × 0.15 = 116.25` | `116.25 / 620 × 1000 = 187.5 → 188` |
| 2026-02 | 7 | 140 | `7 × 25 × 0.15 = 26.25` | `26.25 / 140 × 1000 = 187.5 → 188` |
| 2026-03 | 11 | 200 | `10 × 25 × 0.15 = 37.5` | `37.5 / 200 × 1000 = 187.5 → 188` |
| totals | 49 | 960 | 180 | `180 / 960 × 1000 = 187.5 → 188` |

三月有一条非正 range 差行程（只贡献次数与里程），另有一条零里程行程（贡献次数与能量）。
充电夹具总能量 160 kWh，其中 AC 120、DC 20、未知 20；费用 72，费用已知能量 140 kWh，
总单价 `72 / 140`，DC 占比 `20 / 140 = 1/7`，不等于三个月占比 `(0 + 0.5 + 0) / 3 = 1/6`。

## 充电体验增量（0.2.0 / charging-experience-v1）

状态：Implemented locally（隔离测试及审查通过，未推送/部署）；维护者确认见 spec [#29](https://github.com/wynnok/MileafServer/issues/29)，
实施 #30–#33；完整定义见 [增量 spec](../product/charging-experience-spec.md)。
设计交接原文保留为提案，不作为已实现契约。
冻结契约为 [openapi-0.2.0.json](openapi-0.2.0.json)，是充电增量的发布快照；
运行时契约自统计洞察增量起冻结为 [openapi-0.3.0.json](openapi-0.3.0.json)，只做加法。

### 共用查询与分页

充电列表、`stats?kind=charging`、新增 `GET /v1/vehicles/{vehicle_id}/charging-locations`
接受 `time_basis=started_at|ended_at`、`charging_type=ac|dc|unknown`、`location_id`、
`query`（地点文本，规范化后最多 100 字符）、`cost_status=known|unknown|zero`，条件取交集。
文本对围栏名及地址 name/road/house_number/city/neighbourhood 的空格连接串做大小写不敏感
的字面子串匹配，`%`/`_` 无通配符意义；仅空白的搜索等同省略。
`tz` 默认 UTC，须为 IANA 时区；时间比较仍按 UTC 半开 `[from,to)`。
列表默认开始日；统计和地点分组默认结束日。结束日列表只返回完成会话；统计和地点分组
无论日期依据都只统计完成会话。跨午夜会话整体归入所选依据的日期，不拆能量。
显式未来 `to` 裁剪到请求时刻；不生成未来桶。列表省略的范围侧仍不加限制。
统计、地点分组要求显式范围且最多 730 天，列表不加此上限。

列表新增 `matched_sessions`（完整匹配数，独立于当前页）、`query`、`as_of`；
充电 stats、地点分组也返回 `query/as_of`。query 含 `from/to/tz/time_basis/filters`，
回显有效时间边界；as_of 是表示生成时刻，不是采样时间，不保证跨端点原子快照。
列表/详情/序列仍缓存 30 秒；统计和地点分组实时查询。
这两种实时响应使用弱 ETag，校验完整数据及 query，排除仅生成时间 as_of；历史数据不变时
可返回 304。其余端点的字节级 ETag 规则不变。

列表排序 `started_desc|ended_desc|unit_cost_added_asc|duration_desc`，默认 started_desc。
有效单价仅来自费用非空、入电量 >0 的完成会话；进行中与无效值排在末尾。
duration_desc 同样将进行中及未知时长放末尾；ended_desc 将无结束时间放末尾。
同值均按开始时间倒序、ID 倒序。分页仍为 1–100，默认 20。
充电游标升级为查询绑定的版本化 keyset，绑定车辆、请求范围、时区、日期依据、规范化筛选、
排序及视图；页大小可改，条件变化返回 400 invalid_request。
显式未来上界按原始请求边界绑定，避免翻页时钟前进造成条件误判；每次查询仍回显有效边界。
不承诺上游持续写入时的快照分页。旧充电游标须重新开始；行程游标不变。

### 覆盖与单价

每个充电桶、totals、地点组在原字段之外新增：

| 字段 | 语义 |
|---|---|
| energy_known_sessions / cost_known_sessions | 入电量非空 / 费用非空的会话数，真实 0 参与 |
| cost_known_energy_kwh | 费用非空且入电量非空的入电量之和 |
| duration_min_total / duration_known_sessions | 记录时长分钟合计 / 有效时长次数，不是等待或占位时间 |
| classification_known_sessions | 沿旧 phases 众数口径可分类的次数；无采样才是未知 |
| unknown_energy_kwh | 未知分类的有效入电量之和，不并入 AC |
| ac_duration_min / dc_duration_min / unknown_duration_min | 对应分类的有效记录时长合计 |
| unit_cost_added / unit_cost_grid | 对各自费用非空且 energy>0 的同批会话求和后相除 |

配对单价对象含 `value/numerator_cost/denominator_kwh/eligible_sessions/excluded_cost_sessions`；
最后一项仅计费用已知但对应能量无效的会话。没有有效分母时 value 为 null，两个合计为 0。
详情增加同样的两个对象，进行中时整个对象为 null。
旧 cost_per_kwh 保留 `sum(cost)/sum(added where cost is known)`，包括其可能失配的分子；
客户端应使用新增配对单价。旧能量合计在全缺失时仍为 0，由覆盖计数表达缺失；
有会话但费用全缺失返回 null；无会话返回 0。used<added 不截断，不输出效率/损耗率。

### 地点账目

地点分组使用当前会话关联：优先 geofence_id，其次 address_id，否则 unlocated。
`location_id` 是不透明稳定键；同名不同 ID 不合并，关联变更会改变历史归属。
分组默认 `sort=energy_desc`，也可 sessions_desc，同值按地点键升序；使用独立绑定游标。
响应含 items、next_cursor、total_locations、unlocated_sessions（均为完整匹配集）。
每组含 CH01 指标、identity_kind、Location、representative_session_id、coordinate_source。
代表会话为本组按开始时间/ID 倒序最近有完整坐标者，无坐标时取最近会话；
coordinate_source 有坐标时为 latest_session_position，否则 null。位置为 WGS84 原始 position，
不是站点中心或运营商入口。不因无坐标丢掉地点组。将同一地点键及其他条件传列表/stats 下钻。

### 详情过程质量与原始事实

详情新增 process_metrics，直接对会话范围内的原始采样做 SQL 聚合：
有效功率 ≥0，返回 power_max_kw、power_valid_samples、power_time_weighted_kw、
observed_seconds、uncovered_seconds、max_gap_seconds=300、rule_version=previous-hold-v1。
均值为前值保持：仅相邻时间差 >0 且 ≤300 秒、前值功率有效的区间计权，不外推首尾。
重复时刻不计该零区间，下一段使用同时间 ID 最后一个样本。

soc_windows 首期固定 20–80，返回实际 start_at/end_at、start/end_observed_soc、
elapsed_seconds、status、reason、threshold_precision、覆盖及 observed-threshold-v1。
按时间/ID 找首次有效 SOC≥20，再找随后首次 SOC≥80；不插值。
exact 表示实际正好 20 和 80，否则可用窗口标 crossed。
状态按 incomplete_session、insufficient_samples、not_reached、gap_or_ambiguous、available
判定；缺 SOC、回退、重复时刻、超过 300 秒缺口标 ambiguous，包括下阈值前一采样的跨越区间。
下阈值首点已经 ≥80 不构成窗口。仅 available 给 elapsed_seconds；重复时间不声称精确 0 秒。
record duration_min 不被这些推导改变。进行中不输出最终阈值结果。

详情另含原始 start/end_rated_range_km、start/end_ideal_range_km（km）；进行中终点为 null。
charger 含 fast_charger_present/brand/type、conn_charge_cable、source_sample_at，
来源标 latest_charge_sample；无采样则事实为 null。不根据地点猜测，不覆盖 is_dc。

### 序列、状态事件与支持声明

`fields` 新增以下字段，各字段共用采样时间索引，未请求省略、请求缺值为 null：

| 字段 | 源列 | 单位 |
|---|---|---|
| voltage_v | charger_voltage | V |
| actual_current_a / pilot_current_a | charger_actual_current / charger_pilot_current | A，含义独立 |
| outside_temp_c | outside_temp | °C，室外 |
| rated_range_km / ideal_range_km | rated_battery_range_km / ideal_battery_range_km | km，口径独立 |
| charge_energy_added_kwh | charge_energy_added | kWh，累计可能重置，不是区间增量 |
| battery_heater / battery_heater_on / battery_heater_no_power / not_enough_power_to_heat | 同名列 | 原始 boolean，互不合并 |

points 保持 2–1000（默认 200）；SQL ≤4×points，再 LTTB ≤points，保留原始首尾。
sampling_rule 为 first-field-extrema-then-multifield-lttb-v1；不保证所有原始极值都在图上。
请求加热字段时增加独立 state_events：每字段首值及每次变化（含 null）按时间/ID顺序，
最多 1000 个事件，返回 total_events、truncated、limit、raw-transitions-v1；
截断时不能当完整状态带。布尔值即使出现在采样点中，也不作连续插值。

meta.charging_experience 与详情/序列 capabilities 包含 contract_version、minimum_server_version、
tested_schema_version 和每字段 fields 支持布尔值。最低服务端 0.2.0；兼容基准 migration
20260808090000。仅新增源列是可选能力，缺列或类型不兼容不阻断服务，字段返回 null；
历史无值时即使支持也仍返回 null。现有必需列缺失继续拒绝启动；支持状态在重启后刷新。
不宣称其他 TeslaMate 版本已通过验证。#28 Grafana 生产口径验收仍须人工完成。

## 统计洞察增量（0.3.0 / statistics-insights-v1）

状态：In progress。规格见 [#34](https://github.com/wynnok/MileafServer/issues/34)，实施工单 #35–#41；
设计交接原文保留为 [提案](../mobile-api-proposals/statistics-insights.md)，不作为已实现契约。
`meta.statistics_insights` 独立声明 contract_version `statistics-insights-v1`、最低服务端 0.3.0、
tested_schema_version 20260808090000，以及 `driving_coverage`、`driving_analysis`、
`range_observations`、`parking_observations` 的支持布尔值；不复用 `charging-experience-v1` 的版本号。
新增源列按功能探测，缺列时对应功能为 `unsupported`，不阻断启动、不影响 0.1/0.2 已发布字段。
各接口的字段、单位、方法与阈值说明随对应工单交付，最终冻结与验收见 #41。
契约变更后用 `UV_CACHE_DIR=.cache/uv uv run python scripts/freeze_openapi.py` 重新生成
`openapi-0.3.0.json`，HTTP 测试校验运行时 `/openapi.json` 与该冻结快照逐字段相等。

### 满电等效额定续航观察（ST07 / #39）

`GET /v1/vehicles/{vehicle_id}/range-observations`，Bearer + 车辆关联校验，30 秒私有缓存与
弱 ETag（校验体排除 `as_of`），错误沿统一 envelope 与 X-Request-ID。

| 参数 | 约束 |
|---|---|
| months | 必填，枚举 `6`/`12`，其他值 400 invalid_request |
| to | 可选，必须带时区偏移；先裁到 `as_of`，缺省即 `as_of`；裸时间戳或不可解析为 400 |
| tz | 可选 IANA 时区，缺省 UTC；非法为 400 |

不收 `from`，`bucket` 固定 `month`。窗口按本地日历算：`to` 前一微秒确定最后一个本地月，
首月 1 日 00:00（本地）为 `from`，共 6/12 个月桶，不按 180/365 天。桶按本地月生成后以 UTC
时刻输出；`partial=true` 表示该月被有效截止截断。整月结束请求用下一月月初作半开上界，
此时最后一桶为上月且 `partial=false`。缺月保留显式空桶，数值 null，不插值。

| 字段 | 源列 | 单位与规则 |
|---|---|---|
| 会话候选 | charging_processes.car_id/start_date/end_date | 仅 end_date 非空；按 ended_at 整体归期，跨月会话归入其结束本地月 |
| 采样候选 | charges.charging_process_id/date | 只取会话 `start_date..end_date`（含端点）内的原始采样；范围外采样不是候选 |
| SOC | charges.battery_level | 百分比，固定来源，不 coalesce usable_battery_level |
| 额定续航 | charges.rated_battery_range_km | km，不混用 ideal/estimated；`rated` 是唯一 range_kind |
| 点值 | 上两者同点配对 | km，`rated_battery_range_km × 100 / battery_level` |
| 进行中会话 | charging_processes.end_date IS NULL | `ongoing_sessions`，计 [from,to) 内开始且未结束者，不参与观察 |

有效点要求 SOC 在闭区间 [20,90] 且额定续航 >0 且有限；排除原因按固定优先级计唯一值：
`missing_soc` → `soc_out_of_range` → `invalid_rated_range`（缺、非正、NaN/Inf 同列），
三者之和等于 `excluded_points = candidate_points - valid_points`。

三层等权，全部在 PostgreSQL 内用 `percentile_cont` 聚合，不预舍入：
会话内点值中位数 → 每个本地结束日的会话值中位数 → 每月日值的 P50/P10/P90。
`totals` 直接对全期日值算同样分位数，不平均月份结果。长充高频采样因此不支配趋势。

`coverage` 逐月与总体各一份：candidate_points、valid_points、excluded_points、
candidate_sessions、valid_sessions、valid_days、excluded_by_reason。
`status` 为 available / no_records（无任何候选）/ insufficient_data（有候选但 valid_days=0）/
unsupported（缺源列，优先于数据量判断）；`sample_status` 在 available 时按 valid_days 给
adequate（≥3）或 sparse（1–2），其余为 insufficient_data。
`first_observed_at` 是查询窗口内最早有效原始采样的 UTC 时刻，`first_observed_scope="query"`，
不是新车基线；无有效采样为 null。响应回显 `query`（vehicle_id、from/to、tz、bucket=month、
filters.months、time_basis=ended_at）、`as_of`、`range_kind="rated"`、
`soc_source="battery_level"`、`method="session-day-rated-range-v1"`、`version=1` 与
`thresholds`（soc_min=20、soc_max=90、minimum_valid_days=3、validation=provisional）。
只声称观察：不输出容量、SOH、衰减百分比、出厂基线、等效循环或可行驶距离预测。
预算：单条业务 SQL（≤2 上限）、5 秒 statement_timeout、最多 12 个月桶，不回传原始采样。

### 行程证据筛选与稳定分页（ST03–ST04 下钻）

状态：Implemented locally（#36）。`GET /v1/vehicles/{vehicle_id}/drives` 保留 0.1 的默认时间范围
（省略侧不加限制）、默认页大小 20 与排序 `started_at DESC, id DESC`，新增筛选与回显字段。
匹配规则只有一份实现：`src/mileaf/queries/driving_query.py::match_source`，方法标识
`driving-match-v1`（常量 `DRIVING_MATCH_METHOD`）；ST03/ST04 分组端点构造自己的
`DrivingQueryEcho`（含 `DrivingFilters` 与 `completed_only=true`）后调用同一函数，
其 `evidence_filter` 可直接原样回灌本列表。

| 参数 | 类型与默认 | 约束 |
|---|---|---|
| `from` / `to` | ISO 8601 带偏移，均可省略 | 半开 `[from,to)`；显式未来 `to` 裁剪到请求时刻，裁剪后空范围 400 |
| `tz` | IANA 字符串，默认 `UTC` | 非法 400；只参与回显与游标绑定，时间比较仍按 UTC |
| `distance_min_km` / `distance_max_km` | float，km，可空 | 必须有限；下界含、上界不含；同时给出须 `min < max`，否则 400 |
| `outside_temp_min_c` / `outside_temp_max_c` | float，°C，可空 | 同上；源列 `drives.outside_temp_avg`，负温度按同一半开规则 |
| `energy_status` | `known` \| `unknown`，可空 | 复用既有估算净能量非空语义：`known` = 已完成且 `energy_kwh` 非空 |
| `completed_only` | bool，默认 `false` | `true` 只返回 `end_date` 非空的完成行程 |
| `cursor` / `limit` | 见分页与序列 | `limit` 1–100，默认 20 |

条件取交集。距离或温度为 NULL、`NaN`、`±Infinity` 时不匹配任何数值条件；负原值保留在证据里
（例如 `distance_km = -1`），其排除原因由分组端点的 coverage 解释。进行中行程仍进入默认列表，
但 `ended_at`、`distance_km`、`duration_min`、`energy_kwh`、`consumption_wh_km` 均为 null，
不把临时值当作最终值；`completed_only=false` 也不把它计作完成行程。

响应在 `items`/`next_cursor` 之外新增：

| 字段 | 语义 |
|---|---|
| `query` | `vehicle_id`、规范化有效 `from`/`to`、`tz`、`bucket`（恒为 null）、`time_basis`（恒为 `started_at`）、`filters`；无筛选时 `filters` 为显式空对象，默认值（如 `completed_only=false`）省略 |
| `as_of` | UTC 表示生成时刻，不是数据库历史快照，分页间不保证原子性 |
| `matched_drives` | 完整匹配集计数，独立于当前页与页大小；源数据不变时所有页不重不漏 |
| `method` / `version` / `thresholds` | `driving-match-v1` / `1` / `{}`（匹配规则不含建议阈值） |
| `status` | `available`；无候选为 `no_records` |

游标是查询绑定的版本化 keyset：绑定方法版本、车辆、原始请求范围（裁剪前的 `to`）、`tz`、
全部筛选、`completed_only` 与排序，**不绑定页大小**，翻页时可自由改 `limit`。
上下文不匹配或游标非法返回 400 `invalid_request`。0.1/0.2 的旧行程游标只在请求不含任何新增参数
（`tz` 与全部筛选）时继续兼容，与新增筛选混用返回 400 并要求重新分页。

预算与缓存：一次列表请求发 3 条业务查询（完整匹配计数、keyset 页、批量地点），上限 6，
禁止逐行地点查询；每条 SQL 沿既有 `SET LOCAL statement_timeout='5s'`；响应沿既有 30 秒
进程内缓存与字节级 ETag/304。本切片不引入新源列：`drives.distance`、`drives.outside_temp_avg`
及能量源列已由 0.1/0.2 必需列启动探针覆盖，缺列时启动失败并给出修复指引，不静默降级为
`unsupported`，因此 `meta.statistics_insights.features` 不新增本切片的功能位。
