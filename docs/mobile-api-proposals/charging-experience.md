# Mobile充电体验：待实现接口要求

日期：2026-10-27。状态：**产品方案待评审，接口建议未实现、路径/字段名待Server确认**。本文件是Mobile设计交接，不是对既有API的修改，也不是已批准扩大生产范围。Server工作区只新增此说明文档；不访问任何数据库或生产连接配置。

## 目标与优先级

车主需要看明白期间费用/补能/时长，找到某天某地的一次充电，并理解过程。先补P1「算得清、找得到」，再补P2「过程看得懂」。不要求将Grafana每个面板转换为一个端点；请优先扩展现有列表、stats与detail/series，分组能力另设有界端点。

| 需求ID | 优先级 | 新能力 | 当前为何不足 |
| --- | --- | --- | --- |
| CH01 | P1 | 费用、能量、分类、时长的有效覆盖；有效配对均价 | 现有只有总额/均价/分类能量，没有覆盖字段、总记录时长或显式未知量 |
| CH02 | P1 | 统一日期依据、全查询筛选/排序/匹配集摘要 | 现有列表按开始时间，stats按结束时间；列表仅日期筛选，无法正确从结束日下钻 |
| CH03 | P1 | 稳定地点标识、地点聚合与记录下钻 | 当前仅地点名称、围栏名、地址与坐标；同名不能可靠分组 |
| CH04 | P2 | 原始峰值、时间加权均值、SOC阈值区间时长与质量 | 现有200点采样不保证极值或时间覆盖，不能据其积分/平均 |
| CH05 | P2 | 扩展序列/明细与能力声明 | 现有仅power,soc；TeslaMate有电压、电流、室外温度、加热、续航、充电器字段 |
| CH06 | P3候选 | 开始时段/SOC端点/时长分布、同地点历史 | 当前日桶无法表示跨会话分布；可以后续独立交付，不阻塞P1 |

## 原始数据依据与不可凭空补的内容

静态依据TeslaMate checkout `6bf3d55e4242cf9c55d1dd334b18e35c9ff59d9f`。Server当前核对checkout `72710ff6ea5ee2805ed4a9fdea2854b384a44147`，Mobile冻结契约来源Server `146cb3b9a16af3d632b04ed8d197d0f2243d9b71`。这是静态阅读，不表示已验证各TeslaMate安装版本。

| 输入 | 已核对字段 | 可支持的结果 |
| --- | --- | --- |
| `charging_processes` | id/car_id/start_date/end_date/duration_min；charge_energy_added/charge_energy_used/cost；start/end_battery_level；start/end_rated_range_km、start/end_ideal_range_km；outside_temp_avg；geofence_id/address_id/position_id | 会话、覆盖、时长、有效费用与能量、SOC/续航端点、地点关联 |
| `charges` | date/charging_process_id/charger_power/battery_level；charger_voltage/charger_actual_current/charger_pilot_current/charger_phases；outside_temp；battery_heater/battery_heater_on/battery_heater_no_power/not_enough_power_to_heat；rated_battery_range_km/ideal_battery_range_km；charge_energy_added；fast_charger_present/brand/type、conn_charge_cable | 原始功率统计/阈值时间/过程序列/离散状态/充电器事实 |
| `geofences/addresses/positions` | 已有主键与名称/地址、会话关联position坐标 | 稳定地点身份、标签、原始位置点；不等于运营商站点数据库 |

不提供或不可靠提供：实时桩位、排队/等候、插枪到拔枪占位时长、站点标称功率、运营商账单拆分、实际分时电价历史、电池包温度、加热耗能、车型通用标准20–80成绩。不能用地点名称猜品牌、用全部AC猜家充、用夜间开始猜谷电，或用SOC反推精确容量与衰减。

## 共同契约要求

- 仍是Bearer保护的只读查询。只读TeslaMate，不写入、不迁移其schema、不读取Tesla凭据。补列先声明兼容能力；缺列/缺历史返回支持状态或null，不用假0补齐。
- `from`含、`to`不含，带时区的时间戳；日历按请求IANA `tz`。今天截到当前时刻，不出现未来桶；跨夏令时用本地日历边界。保留现有stats最大730天，列表不强加同一上限。
- 结果返回规范化查询回显：`from/to/tz/time_basis/filters`与`as_of`。`as_of`只表示查询快照生成时间，不冒充Tesla采样时间；跨端点不承诺原子快照，客户端分别标记和刷新。
- 进行中独立于完成统计，不能给最终SOC/费用/时长/20–80确定值。费用0与未知分开；无记录、字段全缺失、空桶有各自语义。币种沿现有站点本地显示标签；本需求不新增换汇或凭设备地区推断币种。
- 保留既有字段/默认语义、统一错误envelope、X-Request-ID、ETag/304与缓存规范。新字段避免让旧客户端反序列化失败；能力声明与具体最小兼容版本一并交付。最终OpenAPI重新冻结后才能接入Mobile。
- 每个聚合必须说明完整查询范围，不从分页结果补算；分页游标绑定所有筛选条件、时间依据、排序和稳定次序，条件不一致返回明确错误。限制搜索长度、分组数量与查询跨度，避免无界原始点传输。

## CH01：费用、能量与时间的可信分母

建议增量扩展 `GET /v1/vehicles/{vehicle_id}/stats?kind=charging` 的totals与buckets；字段名可调整，但以下语义须可表示。

| 结果 / 建议字段 | 定义 |
| --- | --- |
| `sessions` | 匹配查询的已完成会话数 |
| `energy_added_kwh` + `energy_known_sessions` | 非空入电量求和及有效记录次数；全缺失不能只用0让人误认为没有补能，另由coverage标出 |
| `cost` + `cost_known_sessions` | 非空费用求和，0参与；有会话但费用全未知返回null；空查询可保留现有0语义 |
| `cost_known_energy_kwh` | 费用非空且入电量非空的对应入电量之和；不表示这批全部适合算单价（可能能量为0） |
| `unit_cost_added` | 对**同一批**费用非空、added>0的会话，用费用和÷added和。对象包含value、numerator_cost、denominator_kwh、eligible_sessions、excluded_cost_sessions；分母0为null |
| `unit_cost_grid`（次级，可P2） | 对费用非空、used>0的同一批会话计算；与入电均价分开，绝不用两者混合分母 |
| `duration_min_total` + `duration_known_sessions` | 非空`duration_min`求和及覆盖；指记录跨度，不是人的等待/占位时长 |
| `ac/dc/unknown_energy_kwh` + `classification_known_sessions` | 沿当前分类函数、基于同一匹配集明确返回三类有效入电；未知不能归AC，分类无效记录另有覆盖 |
| `ac/dc/unknown_duration_min` | 同一分类下有效记录时长；不平均单次时长百分比 |
| `dc_energy_share` | 沿既有DC/(AC+DC)，分母0为null，明确仅已知分类 |

**兼容提醒**：当前SQL `cost_per_kwh`分子是`sum(cost)`，分母是`sum(energy) FILTER(cost IS NOT NULL)`；费用已知但energy为空时可能失配。不要在Mobile补算，也不静默更改旧字段。新增`unit_cost_added`明确配对并回传被剔除的费用已知记录数；旧字段保留其已发布语义，迁移另行确认。单次详情的两类单价也由服务端按同样规则给出，避免各页面自行定义分母。

不要求本期输出“充电效率/损耗率”。若未来交付能量比，必须在同一批两侧均有效的会话上求和后计算，分别返回覆盖和used<added质量标记，不取max或截断到100%掩盖数据，不称运营商实测效率。

## CH02：能从图找到正确的记录

建议扩展已有 `GET /v1/vehicles/{vehicle_id}/charging-sessions`：

- `time_basis=started_at|ended_at`：省略保留当前started_at。ended_at视图只查询已完成会话，与stats的默认完成口径一致；进行中另通过原有开始日查询展示，不混入结束日匹配总数。
- `charging_type=ac|dc|unknown`、稳定`location_id`、`query`地点文本、`cost_status=known|unknown|zero`；空白搜索规范化，名称/围栏/地址字段匹配规则固定，不逆编码。请求支持多个条件时取交集。
- `sort=started_desc|ended_desc|unit_cost_added_asc|duration_desc`。`unit_cost_added_asc`只有费用非空且added>0可计算，未知/无效值放末尾，不把未知当0；同值再用时间和id排序。游标携带排序值及null类别，不使用offset。
- items仍含现有字段；新增稳定地点身份。返回`matched_sessions`与规范化query，匹配集stats可独立查询。不要每一页重复扫描并返回大聚合；客户端用独立stats加载和恢复。

stats追加同一`time_basis`与所有筛选参数，省略保留当前ended_at；无论选择何种依据，完成统计均排除进行中。完成日下钻的Mobile统一传ended_at，列表/日桶/摘要/地点分组用同一查询；切到开始日模式时明确标注，而不是偷换日期。

必须通过：10/25 23:10–10/26 03:30的会话，在开始日25日查询中出现，在结束日26日查询和26日日桶中出现；26日结束日图→记录不能漏；跨区间的会话只整体归期，不在客户端跨天拆电量。

## CH03：自己的地点账目

建议 `GET /v1/vehicles/{vehicle_id}/charging-locations`，接受CH02同一时间/筛选条件；响应有界分页、总地点数、`unlocated_sessions`、规范化query。每个item至少：

- `location_id`不透明稳定键、`identity_kind=geofence|address|unlocated`、Location标签；规则优先会话geofence_id，其次address_id，都无则unlocated。不同地址ID同名不合并。归属变更会影响历史，服务端明确按当前关联而非不变运营商站点身份。
- 同一匹配集的次数、入电量、已知费用、CH01的覆盖/有效均价、记录时长、AC/DC/未知；按照入电量/次数排序，支持稳定分页。
- 地图代表点标注来源：例如该组最近一条有坐标会话的原始position坐标、对应session_id；不是运营商入口或真实站点中心。无坐标组仍出现在列表。地图坐标来源与现有CRS规则一致，客户端不外部逆编码。
- 点地点将同一`location_id`传CH02，列表匹配集与该组一致。地图如需视口/网格聚合，另返回有界簇；不能把第一页地点冒充全期间空间分布。P1可先只交付地点列表，C地图升级可后置。

费用较低不自动代表划算，快慢需要SOC、方式、温度和样本覆盖；本接口不输出最佳站点评分，不维护外部充电站目录。

## CH04：原始过程统计与20–80用时

建议扩展 `GET /v1/charging-sessions/{session_id}` 的`process_metrics`，从原始charges读取，**不对series接口已下采样结果计算**。

- `power_max_kw`：有效原始charger_power的max；附原始有效样本数。样本无效/全空返回null。
- `power_time_weighted_kw`：明确使用前值保持或后值区间、时间差、最大允许间隔；按有效覆盖时间加权。返回observed_seconds、uncovered_seconds、规则版本/最大间隔，不能均分采样点或默认跨大缺口持续充电。图旁称「记录平均功率」。
- `soc_windows`：请求或固定返回20–80，包含from_soc/to_soc、start_at/end_at、elapsed_seconds、start/end_observed_soc、status、coverage/规则版本。可支持其他阈值但不要求首期任意配置。
- 阈值建议：按时间顺序找首次有效达到下阈值的采样，再找随后首次有效达到上阈值的采样；主例实际恰有20与80。跳过阈值时必须保留实际SOC、标记阈值跨越精度，前端称「记录跨越20–80」，不能假装正好命中或插值成精确值。回退、重复和长间隔如何处理需固定，并返回对应状态。
- `status`至少区分available、not_reached、incomplete_session、insufficient_samples、gap_or_ambiguous；除available外duration可为空并说明原因。进行中首期不输出最终20–80结果。给出真实0秒/重复时间时的明确处理，不能出现负用时。

上面是待后端确认的算法要求，不能只返回一个无质量说明的数字。记录时长沿TeslaMate `duration_min`；原始过程质量不能改变已发布会话的端点事实。

## CH05：更深但仍可读的过程

扩展 `GET /v1/charging-sessions/{session_id}/series?fields=...&points=200`，保留当前power/soc兼容与有界采样。建议字段：voltage_v、actual_current_a、pilot_current_a、outside_temp_c、rated_range_km、ideal_range_km、charge_energy_added_kwh，以及原始boolean加热字段。单位写入契约，不由前端从电压电流重建功率或损耗。

- 同一响应的指标共用时间采样索引，按t升序；各字段未请求可省略，请求但无值为null。显式null断开，0保留，单点可见。连续/离散状态不相互插值；同SOC多点保留原始时间次序。
- 加热状态若降采样会丢切换，应返回有界离散事件/区间或保证转换保留并说明截断，不能只按200个均匀点假装完整状态带。
- 若采样器为多个连续字段做极值保留，需声明选择规则和总点数上限，不保证所有字段原始峰值都在图中；原始峰值仍由CH04提供。
- 明细新增原始rated/ideal起止续航、充电器品牌/类型/cable及来源，不从地点名称猜；`fast_charger_present`原值不替换既有is_dc口径。温度明确是室外；加热字段名称与语义不擅自合并。
- 累计入电charge_energy_added字段可能重置，需保留/标注语义；不把它与一段增量直接混用。充电器pilot/actual电流含义独立，不由差值自动诊断故障。

## CH06：后续分布能力（不阻塞前两组）

有界聚合支持会话开始小时/星期、SOC起止二维或区间分布、时长分位数，返回有效样本数、未覆盖和查询条件。夜间在本原型固定为18:00—06:00开始会话，若服务端提供这一分组必须回显规则。不要用日桶数量反推充电次数分布，也不按采样点数算时长占比。

同地点历史、跨会话DC功率-SOC分布需另外确定可比SOC窗口、温度/方式/充电器记录、样本量、分位数和质量，不用所有DC的均值宣称这个站最快。P1地点账目不依赖该模型。

## 固定合成例与服务端验收要求

主例：2026-10-27 13:12–13:46+08:00，18%→82%，added46.8、used49.5kWh，cost71.28；原始峰值175kW、展示样本峰值166kW；20%13:13、80%13:41，记录用时28分钟。入电均价约1.52、网侧均价1.44（站点示例标签CNY）。这些是设计fixture，不是生产对账数据。

必须覆盖以下检查，使用**新建本地隔离测试库**，禁止继承/读取生产连接配置或向生产装载数据：

1. 正常、无记录、全空能量/费用、0费用、0能量、费用已知能量缺失；覆盖计数与配对均价，旧字段保持兼容。
2. AC/DC/无样本未知、分类中有效/缺失能量、used<added；未知不归AC，不clamp数据。
3. 跨日/月/年、DST与IANA分桶、上界排除、按开始/结束依据、进行中与跨期记录，日图和下钻的完整匹配集一致。
4. 搜索命中未在第一页的记录、未知费用排序末尾、同排序值稳定性、翻页不重漏、改变条件拒绝旧cursor；匹配汇总不随已载页变化。
5. 同名不同地点ID、无围栏但有地址、无身份但有坐标、无坐标、有界分组/分页；地点组与下钻一致。
6. 不均匀采样、真实0、缺列/null、重复时刻、SOC重复/回退/跳阈值、未达到80、单点/空序列、长缺口和进行中；原始极值与采样极值不同；20–80状态正确。
7. 原始序列预算、查询成本、Bearer/车辆归属、404、ETag/304、统一错误与扩展能力缺失；不拉取无界全历史点给手机。

交付时请提供更新OpenAPI、具体源字段/计算定义、能力与版本说明、隔离fixture测试及对账输入。Mobile随后固定新契约，在已确认方案范围接入；本文件不授权推送、部署、数据库写入或关闭真实验收票。
