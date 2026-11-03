# 行笺 / Mileaf 词汇表

本表固定 Mileaf 的领域词汇。写代码、开 issue、写 spec、做 UI 文案时一律使用「术语」一列；「避免」一列的说法不要用。

| 术语 | 代码名 | 定义 | TeslaMate 对应 |
|---|---|---|---|
| 行笺 | Mileaf | 产品名，含 Mileaf Server 与 Mileaf Mobile | — |
| Mileaf Server | mileaf-server | 本仓库：TeslaMate 的默认只读、有限费用补录 API 层 | — |
| Mileaf Mobile | mileaf-mobile | Flutter 客户端，独立仓库 | — |
| TeslaMate | — | 用户自托管的 Tesla 数据记录服务（Elixir + PostgreSQL + Grafana），是唯一数据源 | — |
| 站点 | site | 一套 TeslaMate 部署及其全部数据；一个站点对应一个 Mileaf Server 实例 | 一个 `teslamate` 数据库 |
| 车辆 | vehicle | 被记录的 Tesla，至少一辆，可多辆 | `cars` |
| 行程 | drive | 从一次换挡行驶到驻车的完整区间 | `drives`（避免：trip，TeslaMate 早期叫 `trips`，已更名） |
| 充电会话 | charging session | 从插枪开始充电到拔枪结束的完整区间，含能量、费用、起止电量 | `charging_processes`（避免：charge） |
| 充电采样 | charge sample | 充电过程中按时间记录的功率/电量点，用于曲线 | `charges` |
| 位置点 | position | TeslaMate 按时间记录的 GPS、速度、电量、温度采样，是最大的表 | `positions` |
| 状态区间 | state segment | 「在线 / 休眠 / 离线 / 充电」等状态的连续时间段，用于时间线 | `states` |
| 电量 | state of charge (SoC) | 电池剩余百分比 | `positions.battery_level`、`charging_processes.start_soc`/`end_soc` |
| 续航 | range | 满电或当前电量对应的可行驶里程；分「额定续航」与「估算续航」两种口径，不可混用 | rated range / estimated range 字段 |
| 电耗 | consumption | 每公里耗电（Wh/km）；分「净电耗」与「毛电耗」，图表与统计必须标注是哪种 | `drives` 上的聚合列 |
| 充电功率 | charging power | 充电时的瞬时功率（kW） | `charges.charger_power` |
| 地理围栏 | geofence | 用户命名的圆形/矩形区域（家、公司），用于归类起终点 | `geofences` |
| 地址 | address | TeslaMate 反向地理编码并缓存的地址文本 | `addresses` |
| 固件更新 | firmware update | 车辆 OTA 更新记录，含版本号与安装时间 | `updates` |
| 电池健康 | battery health | 由满充会话与行驶数据推导的容量/续航衰减，是推导值不是实测值 | 由 `charging_processes`、`drives` 计算 |
| 吸血鬼耗电 | vampire drain | 驻车且未充电期间的电量损失 | 由 `positions`、`states` 计算 |
| 时间线 | timeline | 把状态区间、行程、充电会话合并到一条按时间排列的轴上 | `states` + `drives` + `charging_processes` |
| 数据新鲜度 | freshness | TeslaMate 最后一次写入数据的时间；Mileaf 的数据不可能比它更新 | `positions` / `states` 的 `MAX(date)` |
| 指标口径 | metric definition | 一个数字的确切计算式与单位；同一口径在 App 与 Grafana 必须一致 | 见 `docs/product/v1-scope.md` |
| 有限写入适配层 | limited-write adapter | Mileaf Server 的架构定位：默认 SELECT，仅允许显式启用的费用补录，不建表、不改结构、不复制数据 | — |

## 命名约定

- 产品中文名「行笺」，英文名「Mileaf」，代码与包名统一 `mileaf`。不要写成 Mileaf、MiLeaf、MileafApp 混用。
- API 内部一律用本表的代码名（`drive`、`charging_session`、`position`、`state_segment`），不要直接暴露 TeslaMate 的表名给客户端。


> 2026-11-03 边界更新：默认只读，显式启用后允许补录已结束充电会话的缺失费用；唯一写入为 charging_processes.cost。规则、接口与最小权限见 docs/adr/0004-limited-charging-cost-writes.md，替代本文绝对只读的描述。
