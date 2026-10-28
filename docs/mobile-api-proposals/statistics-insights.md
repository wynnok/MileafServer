# Mobile统计洞察：待实现接口要求

2026-10-28。状态：**产品方案待评审，以下路径/字段/阈值为建议，未实现、未冻结契约**。这是设计交接，不授权修改数据库、生产配置或部署。Server维护者可调整接口组织，但应保持下述语义与可验证性。Mobile副本与 `MileafServer/docs/mobile-api-proposals/statistics-insights.md` 保持一致。

## 基线及复用

核对 Server `20308f74115ed7a107c6d185dd96884706b62aa1` / OpenAPI0.2.0；TeslaMate `6bf3d55e4242cf9c55d1dd334b18e35c9ff59d9f`。只阅读源码/schema/Grafana SQL，未连接数据库、未测试实际兼容性。不能据字段存在保证每台车有完整历史。

不要重复实现：现有driving stats支持里程/次数/估算净能量/净毛电耗/最高速度；充电CH01–CH05已提供费用与能量覆盖、配对单价、AC/DC/未知、时长、结束日筛选、地点聚合与详情过程能力。CH06开始时段/SOC/时长分布仍是既有待建需求，本文件引用，不另造同义能力。

| 需求 | 优先级 | 建议接口组织 | 输入确实存在于TeslaMate |
| --- | --- | --- | --- |
| ST02 可信比较与覆盖 | P1 | 扩展driving stats元数据/coverage；可用同接口两次查询，聚合比较可选 | drives.distance、range端点、duration_min、start/end_date、cars.efficiency/settings；positions里程端点可辅助覆盖，但不能代表完整原始能耗 |
| ST03 用车分布 | P1 | `GET /v1/vehicles/{id}/driving-analysis?view=usage` | drives.distance/duration_min/start_date/end_date |
| ST04 条件关联及证据 | P1 | 同端点`view=conditions`，扩展drives筛选/查询回显 | drives.outside_temp_avg、distance、duration_min、rated/ideal range端点及效率 |
| ST07 长期续航观察 | P2 | `GET /v1/vehicles/{id}/range-observations` | charges.date/charging_process_id/battery_level/usable_battery_level/rated_battery_range_km；charging_processes.car_id/end_date；不需要自造SOH字段 |
| ST08 停放窗口观察 | P2 | `GET /v1/vehicles/{id}/parking-observations`及有界窗口分页 | drives/charging_processes时间、range/SOC端点、positions端点odometer；states在线/离线/睡眠区间 |

## 通用契约

- Bearer、车辆归属与现有错误envelope/X-Request-ID、ETag/304/缓存规范一致。只读适配器；兼容能力声明和最小Server版本独立于已发布 `charging-experience-v1`。缺列/缺历史/不支持应返回可区分状态，不能空数组冒充成功无记录。
- `[from,to)`、带时区时间戳、IANA `tz`，目前最大跨度730天。日期按本地日历边界，周一开始；截止当前时刻，不生成未来桶。回显规范化`query={vehicle_id,from,to,tz,bucket,filters,time_basis}`及`as_of`。`as_of`只是查询快照时间。比较两端点非原子快照，不假装同时采样。
- driving默认已完成行程按started_at归期，充电沿现有ended_at口径。进行中单列，不给最终耗能/费用。切片范围外记录仅用于明确的边界上下文，不能计入总量；跨期一趟按既有整体归期，不擅自切分。
- `status=available|no_records|insufficient_data|unsupported`或等价语义；无记录计数0，有记录字段全空的指标null。每项coverage包含候选数、有效数、排除数、候选/有效距离或时长及原因。不要用一个全局coverage覆盖所有不同有效集。
- 服务端在完整查询集聚合，数值不先四舍五入；比率来自同批分子分母，分母0为null。负原值保留，异常样本按公开规则排除并计数，不能clamp隐藏异常。响应有界，不向手机传全部历史原始点。
- 容量和健康算法不在本文件首期。新模型都返回`method/version/thresholds`；阈值最终由Server验证后定稿，本文件建议值不能冒充已验证科学标准。

## ST02：比较与驾驶有效覆盖

增量保留旧totals语义。现有净电耗可能使用全里程作分母、能量仅有效记录贡献，不能静默改旧字段或把它解释成完整能耗。建议新增：

```
driving_coverage: {
  completed_drives, distance_known_drives, distance_known_km,
  net_energy_known_drives, net_energy_known_distance_km,
  duration_known_drives, duration_known_min,
  excluded_by_reason: {...}
}
net_consumption_paired: {
  value_wh_km, numerator_energy_kwh, denominator_distance_km,
  eligible_drives, excluded_drives, method
}
```

paired仅含distance>0、估算能量非null的同批行程。沿当前能量表达式：选定rated/ideal起止差>0且efficiency存在才有估算能量；非正差目前是null，而不是可任意恢复的负能量。如未来能量规则变化必须新版本明确，不在此变更旧API。

比较不一定需要新端点：Mobile可分别读取两次stats原值。新增coverage后，主结论比较使用同口径paired指标和显式质量。建议首期强结论门槛：两期均≥5趟且各期有效距离覆盖≥80%；低于门槛仍显示原值与覆盖，但不输出「更省电」概括。阈值回显，后端维护者可调整。

日期策略须可重现：本月10/1–10/28 19:10对比9/1–9/28 19:10，本周对上周同星期/时间，本年对去年同月日/时间；自定义对之前等本地日数与相同末日进度。月长不足时返回`unaligned_calendar`并显示各自原值，默认不算百分比。跨DST用IANA日历，不能用毫秒差伪装同进度。基期为0的相对变化null，绝对差可保留；基期不足历史不按0。

可选`comparison`封装只做服务端原值与质量组织，返回current/reference两套query和`comparability`，不要求自然语言或AI推断。无比较封装时客户端只做展示差值，不重建统计量/业务能量。

**特别差异**：当前driving `consumption_wh_km_gross`使用期间按结束日充入电池量/驾驶里程；TeslaMate `efficiency.json` 的gross使用drive/charge/position事件间续航损失。两者不同，不能宣称已与所有Grafana gross面板等价。保留旧字段并命名解释，若新增事件法指标另立版本；不拿两者差异当停车耗电。

## ST03：用车节奏、分位数与分组

`driving-analysis?view=usage`建议返回同一query下：

- active_days：本地开始日中有已完成行程的去重日数；partial_today显式。按日热图直接使用现有day buckets，年页可用独立day查询或有界日历响应；不能从month桶推日数。
- driving_duration_min：有效duration_min求和及覆盖，只是记录时长；distance_per_active_day只在分母>0时给值。返回有效距离/时长同批的 `mean_speed_kmh`，不平均每趟平均速度。
- distance与duration各自P50/P90：说明使用`percentile_cont`或其他固定算法；候选/有效趟数与分位数独立。不能将每日/每月中位数再平均成全期中位数。
- 固定距离组 `[0,10) / [10,50) / [50,+∞)` km；每组drives、distance_km、duration_min、paired估算能量/电耗及各自coverage。边界10/50只能出现一组；负距离无效并计数；0km保留趟数但不进入电耗分母。空组0次，比率null。
- weekday分布可P1补充：按本地start_date星期返回次数/里程/时长；不能自动命名为通勤，除非未来有用户定义。

下钻复用 `/vehicles/{id}/drives` 的原有时间/游标；建议加`distance_min_km`含、`distance_max_km`不含、`outside_temp_min_c`含/`outside_temp_max_c`不含、`energy_status=known|unknown`、`completed_only=true`及query回显/匹配数。游标绑定全部筛选和稳定排序；不能在手机过滤已载页。统计组和证据列表共享有效集规则/方法ID；不扩大为驾驶画像或路线推荐。

## ST04：温度与电耗关联

`view=conditions`建议返回有界分组，不要求原始散点传输：

```
temperature_bins: [{lower_c, upper_c, drives, distance_km,
  paired_energy_kwh, paired_distance_km, consumption_wh_km,
  median_drive_distance_km, mean_trip_speed_kmh,
  coverage, evidence_filter, sample_status}]
distance_bins: [同ST03]
```

室外温度用drives.outside_temp_avg，建议5°C半开区间；行程平均温度只是分组标签，不能当完整天气或电池温度。每组电耗=sum有效估算能量×1000/sum同批distance，速度=sum同批distance/sum同批duration；分别给coverage，不用每趟电耗算术均值。条件组≥5趟且≥50km才标`adequate`（建议阈值，需验证），小样本仍可选读但不连成确定趋势/给优劣排名；温度缺失单独计数。

首期提供温度和距离两个视角即可。它们不控制坡度、风、雨、胎压、空调、车速结构或路线；**不输出因果、节能建议、最佳温度、驾驶分数**。UI可比较同组记录，用样本量、里程与单程距离中位数说明混杂条件。若后续加散点，需服务端有界采样并给全量聚合，不从抽样计算摘要。

## ST07：满电等效额定续航观察

这是可解释的长期记录趋势，**不是BMS SOH、出厂续航、额定容量或真实可开距离预测**。建议先只做rated口径，不同时混ideal/estimated。

候选输入：已完成充电会话的charges，`rated_battery_range_km`与固定 `battery_level` 同点配对；优先选择20%≤SOC≤90%、range>0的有效点，计算`range_km * 100 / battery_level`。usable_battery_level不替换battery_level；若以后改用usable必须另名/另版本，不能静默coalesce。20–90只是稳定性筛选建议，需要Server根据真实算法论证，不是通用电池检测标准。

避免一次长充的高频采样支配：建议先每会话取中位数，再按本地日取中位数，最后输出月度日值中位数和P10/P90带；返回原始候选点、有效点、有效会话、有效日、排除原因。少于3个有效日的月份仅点读标小样本，不做连续趋势结论；阈值版本化。全范围与单桶方法一致，不能平均月中位数得到总趋势。

请求显式6月/12月窗口，返回month桶、`range_kind=rated`、`soc_source=battery_level`、`first_observed_at`、方法/阈值、coverage。缺月份断线，不能插值；记录首次值不是新车基线，首期不输出衰减百分比、等效循环数或容量。真实里程可关联positions端点另做有界横轴，非本期必需。

原型长期页仅画合成monthly estimate，展示样本和「非健康检测」说明。不能据此声称已提供容量/保修诊断。

## ST08：可观测停放窗口

候选窗口为同车上一已完成drive/charge结束，到下一drive/charge开始之间的闭合间隔。查询须读取窗口边界前后的相邻事件；排除重叠/进行中/不完整事件穿过窗口、缺端点、里程变化≥1km、时长<1h（建议阈值）。窗口按start归期并回显，跨期窗口不要客户端切分百分点变化；不能默认整段区间有观测。

先实现SOC观察，再选做range估算能量：

- `soc_change_pp = end_battery_level - start_battery_level`，命名为电量变化，负号表示下降；`soc_change_pp_per_24h=change/duration_h*24`只是时长标准化率，不声称会持续一整天。sum变化/sum小时再标准化，不能平均窗口速率。SOC量化步长和温度回弹可能产生0/正变化，保留，不补0或取abs。
- 原始range端点同口径、cars.efficiency有效时可返回估算energy变化，需单独method/coverage；低温usable与battery差异需标记。首期可只返回SOC百分点，避免伪精确kWh。
- states与窗口求交集，online/asleep/offline分别累计，缺状态作为unknown。**offline不是asleep、online不是哨兵/空调**。重叠状态或大跨度无可靠边界按质量状态排除/降级，明确计数。
- 返回可用/候选窗口数、观测小时、excluded_by_reason、桶与完整分页证据（start/end、duration、端点SOC/range来源、里程差、状态时长、quality）。分布以有效观测时长作分母；不能声称全月停车时长或全月总损耗。
- 近实时开放窗口首期不计最终结果。没有闭合窗口显示insufficient_data，不展示「0损耗」。不推断哨兵、召唤、预热或车型故障。

## 固定验收场景与交付物

只用新建本地隔离测试库，禁止读取/继承生产连接配置或装载/DDL/清理生产库。交付至少覆盖：

1. 原有0.1/0.2字段语义兼容；无记录/字段全空/0费用/负原值/0距离、能量与距离不同覆盖的paired分母；全期电耗不等于桶均值。
2. 月末不足、闰年、同进度、基期0/缺历史、DST23/25小时日、半开边界、跨日/月/年的drive与charge不同归期；比较两期query完整回显。
3. 不等采样数、不等行程里程的温度组；正好10/50km与5°C边界；未知均温、稀疏组、质量门槛、全查询集匹配证据与游标不重漏，命中不在第一页的行程。
4. 长充高频点与多日少量点的等权策略；SOC0/低SOC/90上界/缺range、缺月份、少于3日、rated/ideal/usable不可混；没有新车基线不输出衰减。
5. 停放窗口跨查询边界、重叠/未完成事件、缺端点、里程变化、SOC升/降/0、同时间点、睡眠/离线/在线/未知分离、状态重叠、长断联；总速率为sum变化/sum小时。
6. 查询预算、响应点数上限、车辆归属/鉴权、统一错误、条件304和缺能力状态。不得把抽样点或分页结果当完整分析输入。

请提供冻结OpenAPI、能力与版本、每字段源列/单位/方法定义、隔离fixture测试、性能预算与对账输入。Mobile在方案确认后按固定契约接入；真实Grafana对账/真机验收保持开放，不推送/部署或提前关票。
