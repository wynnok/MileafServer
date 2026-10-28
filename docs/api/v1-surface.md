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
| GET | `/v1/vehicles/{id}/charging-locations?from&to&cursor&limit` | 充电地点账目分页（0.2.0 增量） |
| GET | `/v1/charging-sessions/{id}` | 会话详情（能量、费用、AC/DC、温度） |
| GET | `/v1/charging-sessions/{id}/series?fields=power,soc&points=200` | 功率/电量曲线 |

### 统计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/vehicles/{id}/stats?kind=driving\|charging&bucket=day\|week\|month\|year&from&to&tz=<IANA>` | 按桶聚合；`tz` 默认 `UTC`，非法返回 400；`kind=driving` 另有 0.3.0 覆盖/配对/比较质量字段，见末节 |

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

### ST02 驾驶覆盖、配对净电耗与比较质量（#35）

`GET /v1/vehicles/{id}/stats?kind=driving` 只做响应加法：旧字段、旧算法与旧舍入不变，
每个 `buckets[]` 与 `totals` 新增 `driving_coverage`、`net_consumption_paired`、
`comparison_quality`，顶层新增 `query`、`as_of`、`range_kind`；`kind=charging` 响应不变。
`query` 回显 `vehicle_id`、规范化有效 `from`/`to`、`tz`、`bucket`、`filters`（无筛选为显式 `{}`）
与 `time_basis=started_at`；`as_of` 是 UTC 表示生成时刻，不是数据库快照时刻。
有效 `to` 先裁剪到请求时刻，裁剪后为空区间返回 `400 invalid_request`，不生成未来桶。
驾驶 stats 仍是 2 条业务查询（旧聚合 + 覆盖聚合，不含鉴权与车辆查找），各受 5s
`statement_timeout`；桶上限沿旧规则（day ≤732、week ≤106、month ≤25、year ≤3）。
该响应改用弱 ETag，校验除 `as_of` 外的完整规范化表示（含 `query` 与三个质量族），命中返回 304。

| 字段 | 单位 / 类型 | 语义与算法 |
|---|---|---|
| driving_coverage.status | 枚举 | `available`（有已完成候选）/ `no_records`（无候选）/ `insufficient_data` / `unsupported`；进行中行程只计入 `ongoing_drives`，不进入任何最终指标 |
| driving_coverage.method / version / thresholds | 字符串 / 整数 / 对象 | `driving-coverage-v1`、`1`、`{}`（无阈值也返回空对象） |
| completed_drives / ongoing_drives | 次 | `end_date` 非空为完成；候选按 `started_at` 整体归期，跨期行程不切分能量或时长 |
| distance_known_drives / distance_known_km | 次 / km | 距离非空且有限的行程数及其原始距离和；负值算「已知」，不算有效 |
| net_energy_known_drives / net_energy_known_distance_km | 次 / km | 估算净能量非空有限的行程数，以及这些行程中距离有限的里程和 |
| duration_known_drives / duration_known_min | 次 / min | 记录时长非空的行程数与时长和，不回退为时间戳跨度 |
| distance / net_energy / duration | 对象 | 三个不同有效集各自的 coverage：`candidate_count`、`valid_count`、`excluded_count`、`candidate_distance_km`、`valid_distance_km`、`candidate_duration_min`、`valid_duration_min`、`unknown_distance_count`、`unknown_duration_count`、`excluded_by_reason` |
| *.excluded_by_reason | 对象（整数计数） | 有效距离为有限且 ≥0，有效时长为有限且 ≥0；每条被排除记录只有一个原因，`candidate_count = valid_count + excluded_count = valid_count + Σ excluded_by_reason` |
| driving_coverage.excluded_by_reason | 对象 | 按 `distance`/`net_energy`/`duration` 分族给出上述计数，不用全局 coverage 代替，也不跨族累加 |
| net_consumption_paired.method / version | 字符串 / 整数 | `paired-net-consumption-v1`、`1`；`thresholds.distance_exclusive_min_km = 0` |
| net_consumption_paired.value_wh_km | Wh/km，可空 | `numerator_energy_kwh × 1000 / denominator_distance_km`，不预舍入；分母为 0 或缺失时 null |
| numerator_energy_kwh / denominator_distance_km | kWh / km，可空 | 同一批 `distance > 0` 且估算净能量非空有限的已完成行程的分子与分母，沿旧 `preferred_range` 能量表达式；无候选为 0，有候选但无有效值为 null |
| eligible_drives / excluded_drives | 次 | 配对有效行程数与被排除数，两者之和为 `completed_drives` |
| net_consumption_paired.coverage | 对象 | 配对有效集自己的 coverage，原因按 `missing_distance` → `invalid_distance` → `non_positive_distance` → `missing_energy` → `invalid_energy` 优先级取唯一值 |
| comparison_quality.method / version | 字符串 / 整数 | `paired-comparison-quality-v1`、`1` |
| comparison_quality.thresholds / validation | 对象 / 枚举 | `minimum_drives = 5`、`minimum_distance_coverage = 0.8`，`validation = provisional`：首版产品筛选门槛，不是统计显著性标准 |
| comparison_quality.sample_status | 枚举 | 配对有效行程 ≥5 且 `distance_coverage ≥ 0.8` 且分母 >0 时 `adequate`，否则 `insufficient_data`；功能不支持时 `unsupported` |
| comparison_quality.distance_coverage | 比率，可空 | 配对距离 ÷ 同批候选的有效已知总距离；分母为 0 或缺失时 null，null 不能达标 |
| eligible_drives / unknown_distance_drives / invalid_distance_drives | 次 | 比较输入的诊断计数，另列不参与加和 |
| range_kind（顶层） | `rated` \| `ideal` \| null | 本次采用的 `settings.preferred_range`；`unsupported` 时为 null。不同 `range_kind` 的两期不是同口径 |

`driving_coverage` 能力位在 `/v1/meta.statistics_insights.features.driving_coverage` 暴露，
由 `STATISTICS_REQUIREMENTS["driving_coverage"]` 注册的源列（`drives.duration_min`，int2/int4）探测。
该列同时是既有启动探针的必需列，因此缺列时服务按 ADR-0002 拒绝启动（fail closed），
不会退化成 `no_records`；三个质量族仍保留 `unsupported`，用于源列可选时的按功能降级，
并与 `no_records`（无候选）、`insufficient_data`（有候选但无有效输入）严格区分。

旧 `consumption_wh_km_net` 仍用旧能量表达式与全部里程分母，`consumption_wh_km_gross` 仍按
结束日入电量 ÷ 驾驶里程并保留旧舍入；两者与 `value_wh_km` 不同口径，不得互相替代，
净毛差也不解释为停放耗电。

固定夹具手算（`rated`、efficiency 0.15 kWh/km，`2026-03-02T00:00Z`–`2026-03-06T00:00Z`）：

| 期间 | 完成行程 | 已知里程 km | 能量已知 | 配对分子 kWh | 配对分母 km | value_wh_km |
|---|---:|---:|---:|---:|---:|---:|
| totals | 3 | `0 + 20 + 20 = 40` | 2 | 3.75 | 20 | `3.75 × 1000 / 20 = 187.5` |

零里程行程贡献次数与能量但不进配对分母（`non_positive_distance`）；非正 range 差行程能量未知
（`missing_energy`），因此 `distance_known_drives = 3` 而 `net_energy_known_drives = 2`。
全期值不等于桶均值：把 id 18 的里程改成 10 km 后，1 月 1 日桶 `3.75 × 1000 / 10 = 375`、
1 月 2 日桶 `187.5`，totals 为 `7.5 × 1000 / 30 = 250`，不是 `(375 + 187.5) / 2 = 281.25`。

#### Mobile 两次查询比较接入

首版不提供 comparison 端点，也不做服务端相对变化。Mobile 对同一车辆发两次
`stats?kind=driving`（当期与参考期），并遵守：

- **日历对齐**：周对上周同星期同本地时刻，月对上月同日同本地时刻，年对去年同月日同本地时刻；
  自定义区间按触及的本地日期数前移相同日期数，并保持起止各自的本地时刻。目标月日不存在
  （含闰日）时裁到目标月末并标 `unaligned_calendar`；DST 不存在或重复的本地时刻同样标
  `unaligned_calendar`，不以毫秒差代替日历。非对齐区间仍可查询原值，默认相对变化为 null。
- **非原子快照**：两次请求各有自己的 `query` 与 `as_of`，服务端不保证跨请求或跨分页的原子快照；
  两端都必须保留 `query`/`as_of` 以便追溯，期间上游写入可能造成轻微不一致。
- **零基期与缺历史**：参考期为 0 时相对变化为 null；参考期无记录或历史不足时不得当作 0。
  只有两期都 `sample_status = adequate`、同 `range_kind`、同 `bucket`、日历对齐且进度相同时，
  才展示绝对差 `current − reference` 与相对差 `(current − reference) / reference`。
- **不下强结论**：`method`、`version` 或 `range_kind` 不同、任一期不达标、日历不对齐时，只展示
  原值与质量状态；服务端不输出自然语言判断、评分、排名或建议，Mobile 也不得代为生成。
- 新增比较输入（分子、分母、覆盖计数）均未预舍入；Mobile 只做日历组织与展示差值，
  不得用分页数据或桶比率重算能量与统计量。
