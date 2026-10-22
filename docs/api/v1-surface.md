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
| GET | `/v1/vehicles/{id}/drives?from&to&cursor&limit` | 行程分页列表 |
| GET | `/v1/drives/{id}` | 行程详情（含起终点地址与围栏、温度、速度极值） |
| GET | `/v1/drives/{id}/series?fields=speed,battery,elevation&points=200` | 图表序列，服务端降采样 |

### 充电

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/vehicles/{id}/charging-sessions?from&to&cursor&limit` | 充电会话分页列表 |
| GET | `/v1/charging-sessions/{id}` | 会话详情（能量、费用、AC/DC、温度） |
| GET | `/v1/charging-sessions/{id}/series?fields=power,soc&points=200` | 功率/电量曲线 |

### 统计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/vehicles/{id}/stats?kind=driving\|charging&bucket=day\|week\|month\|year&from&to&tz=<IANA>` | 按桶聚合；`tz` 默认 `UTC`，非法返回 400 |

延后屏幕的端点先占位、不实现：`/states`、`/timeline`、`/battery-health`、`/updates`、`/locations`。

## 分页与序列

列表游标为 `base64url("<started_at>|<id>")`，客户端不得解析；非法编码或内容返回 400。按 `(started_at, id)` 倒序，以严格小于游标键的记录继续翻页。`limit` 必须为 1–100，默认 20，超出范围返回 400。

行程与充电会话列表响应为 `{"items": [...], "next_cursor": null}`。`from` / `to`
均可省略，省略的一侧不限制时间；提供时必须带时区，且同时提供时必须满足 `from < to`。
列表按开始时间筛选，进行中的记录也可返回；其结束时间、最终时长、最终电量为 null，
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
`consumption_wh_km_gross`、`speed_max_kmh`。`charging` 固定键：`sessions`、
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

充电按已结束会话的 `end_date` 归桶，`energy_added_kwh` 合计 `charge_energy_added`。
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
