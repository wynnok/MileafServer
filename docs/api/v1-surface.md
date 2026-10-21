# Mileaf API v1 接口面

契约先于实现：本文件是 `/to-spec` 的输入，实现为 FastAPI（见 ADR-0001），但契约本身与语言无关。OpenAPI 3.1 是最终事实来源，客户端由它生成 Dart 代码。

## 设计原则

- 只读资源导向：除认证外全部 `GET`。
- 一切查询都带时间范围，默认值明确，不做「全表返回」。
- 服务端只做聚合、裁剪、降采样，不重算 TeslaMate 已经算好的指标（见 ADR-0002）。
- 客户端拿到的每个 DTO 都自带 `freshness`，不依赖额外请求判断数据新旧。
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
| 电耗 | Wh/km，字段名必须带 `_net` 或 `_gross` 后缀 |
| 时长 | 分钟（整数），字段名 `duration_min` |
| 分页 | `limit`（默认 20，最大 100）+ `cursor`（不透明串）；响应含 `next_cursor`，`null` 表示到底 |
| 排序 | 默认按 `started_at` 倒序，同时间按 `id` 倒序 |
| 可空 | TeslaMate 尚未回填的聚合值返回 `null`，不用 0 代替 |
| 缓存 | 列表与详情返回 `ETag`；`Cache-Control: private, max-age=30`；概览 60 秒 |
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
| GET | `/v1/meta` | 服务版本、TeslaMate migration 版本、`schema_ok`、单位制、freshness |

### 车辆

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/vehicles` | 车辆列表，含当前状态、电量、续航、freshness |
| GET | `/v1/vehicles/{id}/summary` | 首页概览聚合（一次请求拿全） |

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
| GET | `/v1/vehicles/{id}/stats?kind=driving\|charging&bucket=day\|week\|month\|year&from&to` | 按桶聚合，返回每个桶的指标 |

延后屏幕的端点先占位、不实现：`/states`、`/timeline`、`/battery-health`、`/updates`、`/locations`。

## 关键响应形状

概览（`/v1/vehicles/{id}/summary`）：

```json
{
  "vehicle": {"id": 1, "name": "Model 3", "model": "3", "vin_suffix": "4821"},
  "state": {"value": "online", "since": "2026-10-21T12:00:00Z"},
  "battery": {"level": 78, "range_km": 412, "range_kind": "rated"},
  "odometer_km": 42123,
  "period": {
    "today": {"drives": 2, "distance_km": 34.2, "energy_kwh": 5.6},
    "month": {"drives": 18, "distance_km": 412.3, "energy_kwh": 68.1}
  },
  "last_drive": {"id": 9021, "started_at": "2026-10-21T08:12:00Z", "distance_km": 12.3, "consumption_wh_km_net": 168},
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
  "consumption_wh_km_net": 168,
  "consumption_wh_km_gross": 184,
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
| `/summary` | 400ms | 多条子查询，允许 60s 缓存 |
| 列表类 | 300ms | 强制分页 + 覆盖索引 |
| 详情类 | 500ms | 单行 + 关联 |
| `/series` | 600ms | 固定返回 ≤ 200 点，SQL 内降采样 |

## 待定（spec 阶段定）

1. 分页游标编码：不透明 base64 还是 `started_at|id` 明文。
2. 序列降采样算法：LTTB 还是时间桶均值（影响曲线形状与 SQL 复杂度）。
3. token 有效期与轮换策略。
4. 统计的桶边界按 UTC 还是车辆本地时区。
5. `stats` 是实时聚合还是进程内 60s 缓存。
