# 车主体验复核：后端能力交接

日期：2026-10-29。状态：**待后端实现的需求交接，路径、字段、阈值和预算尚未冻结**。用户已授权将所需接口文档写入 Mileaf Server，接口补充留在后端项目后续完成。本次仅写文档，不实现接口、不变更数据库、认证或配置、不部署。

来源：Mileaf Mobile `docs/product/owner-api-requirements.md` 与 `docs/product/owner-experience-review.md`，本地提交 `bac963e`（尚未推送）；本交接落入 Server 基线 `86bf3111606aca9277e26a3024098c29b2c8f152`。不依赖 Mobile 文档相对路径即可阅读下列需求。

现有契约以 [OpenAPI 0.3.0](../api/openapi-0.3.0.json) 和实际路由/能力声明为准；[统计洞察交接](statistics-insights.md)与[充电体验交接](charging-experience.md)保留历史需求依据，不把其早期“待实现”状态当成当前能力缺失。遵守 [ADR-0002](../adr/0002-read-only-teslamate-adapter.md)：只读查询，不改 TeslaMate schema，不复制 Grafana SQL，不接触 Tesla 凭据。

## 建议实施顺序与移动端用途

以下顺序是本轮产品复核的建议，不代表已建工单、冻结排期或完成验收。后端维护者可调整接口组织，需保持用户问题、指标语义、证据链和兼容边界。

| 顺序 | 能力 | 移动端用途 | 与已有能力关系 |
| --- | --- | --- | --- |
| OE01 | 电量历史与统一事件 | 概览SOC趋势、行程/充电/状态时间线 | 复用单次详情及停放窗口，不重算既有停放聚合 |
| OE02 | 环境与胎压观测 | 车况补充、内外温历史 | 增量补 summary/观测序列，不把均温当历史 |
| OE03 | 地点回顾与访问地图 | 行程Tab常去地点、访问地图及记录下钻 | 复用充电地点接口；新增行驶目的地与有界空间聚合 |
| OE04 | 补能习惯 | 星期×小时热图、SOC分布、时长分位 | 承接已有CH06，不重复实现CH01–CH05 |
| OE05 | 行驶过程补足 | 功率、温度、胎压、爬升及速度结构 | 扩展 drive series；回收能量先验证算法再另行冻结 |
| OE06 | 固件履历 | 车辆资料内的升级历史 | 复用 summary 当前固件，不加入OTA控制 |

## 已有能力直接复用

0.1 summary、drives/detail/series、charging-sessions/detail/series、stats；0.2充电覆盖/配对均价/地点/过程字段；0.3 driving_coverage、driving-analysis的usage/conditions、range-observations、parking-observations与窗口证据。已有统计不另造一个owner-dashboard端点；先改善移动信息架构和呈现，避免聚合层重复口径。

## 共用契约

所有路径为建议名，统一在`/v1/vehicles/{id}`下，沿用Bearer、车辆归属、错误envelope、X-Request-ID、ETag/304和缓存TTL。兼容版本与能力声明逐项扩展，不以版本字符串推定全部支持；缺表/列返回unsupported，不返回成功空数组。

查询区间`[from,to)`，带偏移时间戳和IANA tz，跨度与点数有上限，`to<=as_of`。返回规范化query、as_of、source_last_at、method/version、validation、各指标独立coverage、status。as_of不是车辆采样时间。单位写进契约，币种沿站点显示设置，不自行换汇。

0、null、无记录、字段全空、稀疏和不支持必须可区分。无记录不填虚假未来桶，断联不插值。所有聚合由服务端完整查询集计算；证据分页不改变totals，游标绑定车、区间、时区、筛选、排序和方法版本，权限每页校验。

## OE01：电量历史与统一事件（最高优先）

**问题**：最近电量怎样变化？下降发生在行驶、补能还是可观测停放窗口？

建议`GET /battery-history?from&to&tz&max_points=480`，区间先限31天；`GET /activity-timeline?from&to&tz&limit&cursor`，区间先限90天。点数/区间为交接建议，需后端性能验证后冻结。

来源：positions的date、battery_level、usable_battery_level、rated_battery_range_km、ideal_battery_range_km、odometer；drives/charging_processes起止、states的online/asleep/offline区间。SOC与usable、rated与ideal各自成列，不能coalesce。positions不覆盖的充电时段是否补charges需明确来源标识、冲突优先级和同点去重，不伪称一个传感器流。

历史返回有界真实观测点`{at,soc_pct,usable_soc_pct,rated_range_km,ideal_range_km,source,source_id}`及gap区间、原始/输出点数与降采样方法。优先保留事件边界和极值；合成连线不补成观测。返回独立行程/充电会话事件引用，点击已有详情；开放事件显式ongoing。事件窗口聚合已有parking-observations继续复用，不通过电量历史重算停放损耗。

时间线返回稳定event_id、kind、started_at、ended_at、状态、记录引用与next_cursor；边界跨期返回交集和原边界，统计归期沿各资源既有定义。SOC百分点变化只能针对明确的同源端点，不用bucket均值相减声称精确耗电，更不换算哨兵/空调耗能。

移动呈现：概览小型SOC曲线+事件带，下钻时间线；真实断点可见。验收：驾驶和充电跨期、同时间戳、来源冲突、长断联、进行中、null/0、max_points、乱序去重、分页不重漏及切车权限。

## OE02：车况环境和胎压

**问题**：这次车况记录包含哪些环境数据，何时采到？

summary可增量返回`environment`和`tire_pressure`，或独立`GET /vehicle-observations/latest`；字段必须带各自sampled_at/source，不能把不同时间的非空值拼成“同时车况”。来源positions的inside_temp、outside_temp、driver_temp_setting、tpms_pressure_fl/fr/rl/rr；实际冻结前核对支持TeslaMate schema与单位。

建议`GET /environment-history?from&to&tz&max_points=480`，限31天，室内外温分系列；日聚合可补min/max/时间覆盖，不以不规则采样算术均值冒充时间均温，未定义时间加权时不提供该值。稀疏日不画密实包络。

移动呈现：车况展开显示四轮位置和压力原值/单位/采样；环境专题双线共用°C轴。没有轮胎推荐压力与温度补偿输入时，不上红色报警、不诊断漏气。验收：部分轮缺值/旧值、混合时间、缺列兼容、温度负值/真实0与断点。

## OE03：地点回顾与访问地图

**问题**：常去哪里？哪一段生活占据我的里程和时间？

建议`GET /driving-locations?from&to&tz&sort=visits_desc&limit&cursor`与`GET /visited-areas?from&to&tz&zoom&bbox`。来源drives起终点position/address/geofence关联；充电地点聚合沿既有charging-locations，不能用AC/DC判断家庭/公共地点。

定义一次到访为已完成drive的终点事件；起点不再算一次到访。location_id稳定绑定服务端围栏或地址ID；同显示名不同ID不合并，未知单列。返回次数、相关行程里程/时长与覆盖、最近到访、可用质心及完整集排名；里程表示“以此为终点的行程距离”，不能叫该地点内行驶里程。

地图按有界空间网格或聚类返回count、聚类范围、查询精度与截断状态，不下发全历史坐标；放大再查，不能把聚类中心叫实际停车点。证据筛选扩展drives的destination_location_id，游标绑定该ID，地图与列表一致。隐私沿既有本地站点访问，不增加外部轨迹上传。

移动呈现：行程Tab的常去地点入口/地图，地点排行和记录互通。验收：同名围栏、围栏后改名/删除、地址未知、跨日/跨时区、零距离、空间边界、全量计数不依赖第一页。

## OE04：补能习惯（承接原CH06，不重复建票）

**问题**：通常什么时候补电、一次补多少、占用多长记录时间？

建议既有charging stats扩展`view=habits`或独立`GET /charging-analysis?view=habits&from&to&tz`；需明确一个选择后冻结，不同时维护两种语义。

来源charging_processes的start_date/end_date、duration_min、start_battery_level/end_battery_level与charge_energy_added；响应字段沿既有energy_added_kwh命名。完整会话按结束日选入，与现有账本一致。星期×小时以该会话本地开始时间分组，返回7×24有界格子的sessions、energy_added、各自有效覆盖；明确“结束日入选，按开始时间分组”，不能悄悄切换日期口径。未完成会话独立，不计最终结果。

返回起SOC/止SOC独立固定分箱（0–100%、末箱含100）、补入能量和记录时长P50/P90、各自候选/有效/排除数及percentile_cont方法。不要由SOC差算入电kWh，不用时长猜用户等待。可加AC/DC/未知及地点过滤，沿现有过滤语义。

移动呈现：补能专题热图可切次数/入电，SOC区间与时长分布使用固定尺度；点击格子要有服务端匹配筛选，不先做不可核查的假下钻。验收：DST重复/缺失小时、跨夜充电、100%边界、0/null、极长/负时长、完整样本分位不等于桶分位平均。

## OE05：行驶过程补足

**问题**：路线中速度、功率、坡度与环境怎样对应？

在既有drive series增量能力字段：power、inside/outside_temperature、tpms四轮；来源positions，单位kW/°C/压力单位明确。详情可加已有drives.ascent/descent及各自质量；速度直方图需服务端按观测时长加权，而不是点数计数，返回已覆盖秒数和缺口。

回收能量不能把负功率点求和；若实现，需明确功率方向、真实时间间隔、最大可积分gap、有效覆盖、停车采样排除与积分方法。覆盖不足返回insufficient_data，且不得反写旧续航差估算能量。先交付功率原曲线，恢复能量指标须算法验证后单独冻结。

移动呈现：详情复用同一过程图的指标切换或同时间轴小图；不默认速度、电量、功率、海拔四轴叠加。验收：采样不均、间断、负功率、单点、截断、点选时刻与单位，不从抽样曲线推导精确能量。

## OE06：固件履历

建议`GET /software-updates?limit&cursor`，来源updates.start_date/end_date/version，返回记录时间、完成/进行中与上一条完成记录间隔及覆盖。相邻间隔用全量前驱，分页首条不能丢掉前驱；当前版本沿summary。

移动呈现：车辆资料内的更新履历；可与期间行驶数据并置，但不声称固件造成电耗变化。不采集官方未提供的变更日志，不做OTA控制。验收：相同版本重复记录、进行中、缺失end、排序稳定和跨页前驱。

## 后端交付验收

仅使用新建隔离测试数据库，禁止继承生产连接配置。交付版本化OpenAPI、meta能力、每字段源列与单位、方法/阈值、覆盖/错误状态、SQL权限与查询预算、固定合成样本、跨DST及分页测试、730天/大表规模的执行计划证据（各接口取自身更小的区间上限）。

Mobile接入后在既有App＋可控HTTP边界验证能力门控、查询回显、0/null、切车/时区竞态、缓存304/失败快照；正式Flutter渲染验证浅深色和大字。真实TeslaMate/Grafana对账、真机读屏/手势和网络环境继续单独验收。

## 后续后端任务开始前核对

1. 重新读取当前代码、OpenAPI、meta能力声明及已有工单，确认 OE01–OE06 哪些仍缺失；不按本交接日期冻结后的旧状态重复建设。
2. 先确定端点组织、字段源列与单位、聚合有效集、时间归期、方法版本、状态与性能预算，再冻结新增OpenAPI；已有0.1/0.2/0.3字段语义保持兼容。
3. 以新建本地隔离测试库完成固定样本、缺失/零值、时区/DST、权限、分页、缓存和规模查询验证；不读取或继承生产连接配置，不对生产库写入、执行DDL或清理，不给TeslaMate加索引或其他结构。
4. 交付接口实现、契约快照、能力声明、来源与方法说明、测试和性能证据，再交接 Mobile 接入；真实站点/Grafana对账与真机体验仍单独验收。

本次文档落地不创建或关闭工单，不承诺服务版本号、上线日期或上述接口已可调用。

## 后续生命周期记录（2026-10-29）

本交接保留为历史需求输入。后端本轮重新核对后发布增量spec [#42](https://github.com/wynnok/MileafServer/issues/42)
及实施票 [#43](https://github.com/wynnok/MileafServer/issues/43)，冻结决定见
[车主体验增量契约](../product/owner-experience-spec.md)，运行时契约见
[OpenAPI 0.4.0](../api/openapi-0.4.0.json)。本轮采用positions单源历史、独立charging-analysis habits，
回收能量继续等待独立算法冻结。原文中的建议值与端点组织以增量契约为准。
验证证据见[本地测试记录](../testing/owner-experience.md)；不代表部署或真实站点验收。
