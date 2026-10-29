# 车主体验增量契约

- 日期：2026-10-29；状态：Implemented locally（隔离测试与双轴审查通过；未推送/合并/部署）；Tracker：spec #42、impl #43。
- 输入：`docs/mobile-api-proposals/owner-experience.md`；基线：`86bf3111606aca9277e26a3024098c29b2c8f152`。
- 生命周期：Draft → Accepted → Implemented locally。实施票 Ready → In progress → Implemented locally；仅在测试与双轴审查通过后更新最终状态，保留真实站点验收 #28。
- 本文作为旧 spec #2–#5、#29、#34 的增量，不覆写旧契约。CH06 归本轮 OE04，不重复建设 CH01–CH05。

## 冻结决定

通用：路径在 `/v1/vehicles/{vehicle_id}` 下；沿用 Bearer、车辆404、错误信封、请求ID、只读连接与每条SQL 5秒超时。显式 from/to 为带偏移时间戳，半开区间，tz 默认UTC且必须为IANA；to裁剪至as_of，空或反向有效范围400。历史31天、时间线90天、地点/习惯730天。各端点返回 query/as_of/source_last_at/method/version/validation/status；validation=synthetic_only，不冒充真实Grafana验证。能力逐项探测可选列类型；不支持返回 status=unsupported 的明确响应，不伪装无记录。短缓存30秒并支持ETag/304；权限与车辆检查在缓存前执行。方法阈值暂定（provisional）。单请求业务SQL≤4条（不计车辆查找），不无界取回原始记录。

### OE01

- `battery-history?from&to&tz&max_points=480`（2–1000）：只使用positions，不混入charges。相同时刻按最大id去重，字段soc_pct/usable_soc_pct/rated_range_km/ideal_range_km独立，source=position、source_id为位置点id。原始/去重/输出点数分别返回；SQL时间桶首尾/主字段极值＋LTTB，上限4×max_points中间行，最终≤max_points。保留首尾；桶内极值优先但不承诺保留所有事件边界或所有极值，独立事件引用由时间线获取。连续真实点间隔>300秒记gap（含查询边缘无观测段），至多1000个，超额显式truncated；不得从降采样后点间距判断原始gap。不推导停放耗电。
- `activity-timeline?from&to&tz&limit=20&cursor`：合并drive/charging_session/state_segment，窗口相交（start<to且end为空或end>from），返回原始与裁剪边界、ongoing/completed、稳定kind:id及记录引用；state_segment额外返回state=online/asleep/offline，其他kind为null。按原始start倒序、kind、id倒序，绑定查询的keyset游标；无跨请求快照承诺。

### OE02

- `vehicle-observations/latest`：positions最新一条记录的环境与四轮压力，统一sampled_at/source/source_id；不回填旧非空值。各指标返回value/unit/sample时间/coverage/status；单位°C、bar（TeslaMate存储单位，不跟显示偏好转换）。环境为inside_temp/outside_temp/driver_temp_setting；胎压为tpms_pressure_fl/fr/rl/rr。
- `environment-history?from&to&tz&max_points=480`：同OE01的真实positions降采样与gap规则，输出inside_temp_c/outside_temp_c；不计算日均温。

### OE03

- `driving-locations?from&to&tz&sort=visits_desc&limit=20&cursor`：仅已完成且开始时间落区间的行程（沿drives下钻口径），终点代表一次访问。身份复用充电地点的base64 geofence/address/unlocated，终点关联优先围栏；改名不改id，删除后遵循当前关联。完整集按次数降序、location_id排序；返回visits、有效非负有限distance_km/记录duration_min合计及独立有效数、last_visited_at（结束时刻）、最近终点标签/坐标（明确代表点，不是质心）、完整集rank。未知单列；总组数与未定位行程数不受分页影响。
- drives增加destination_location_id筛选，绑定游标，完整匹配集与地点一致；不引入结束日默认口径。
- `visited-areas?from&to&tz&zoom&bbox`：同一批完成行程终点，WGS84经纬度等角网格，格宽=360/2^zoom（zoom 0–16）；bbox必填west,south,east,north，不支持跨日界线，边界半开。返回至多1000格，完整格数、truncated、每格count/网格范围/中心，中心仅为网格中心。无坐标行程独立计数；不是精确停车点，非Web Mercator地图瓦片。

### OE04 / CH06

- 仅 `charging-analysis?view=habits&from&to&tz`，≤730天。不新增重复stats view；本轮不提供交互筛选或热图下钻，避免承诺无法核对的下钻。
- 完成会话按结束日选入；本地开始时刻的ISO星期1–7、小时0–23分168格，DST重叠小时合并、缺失小时为0。格内sessions与有效非负energy_added_kwh独立覆盖。进行中会话按开始日入选，独立计数，不进入结果。
- 起止SOC各10个固定[0,10)…[90,100]箱；有效范围0–100。能量与duration_min用完整有效非负样本percentile_cont P50/P90（0有效、负数排除），每指标candidate/valid/excluded。不用SOC差换算能量。

### OE05

- 原drive series增量fields=power,inside_temperature,outside_temperature,tpms_fl/fr/rl/rr，输出power_kw、inside_temp_c/outside_temp_c、tpms_*_bar；未请求省略，请求不支持为null并在field_support标记，真实0/负功率保留。仍共用采样索引与既有点数边界。
- drive详情增加ascent_m/descent_m及各自status（非负有效），可选列不阻止旧端点启动。
- 新增 `/drives/{drive_id}/speed-histogram`：原始positions前值保持、0<dt≤300秒、不外推，重复时间取最大id；仅行程起止内区间。固定速度箱[0,30)、[30,60)、[60,90)、[90,120)、[120,+∞) km/h，负/null不计权；返回observed_seconds/uncovered_seconds、coverage、method。不从抽样曲线推导能量。回收能量另行算法验证冻结，明确不在本轮范围。

### OE06

- `software-updates?limit=20&cursor`：按start倒序、id倒序，当前车辆全历史，原始version不合并，进行中保留。previous_completed_at与interval_seconds由完整集合中结束时间严格早于当前开始的最近完成记录计算（不受分页影响）；没有前驱为null并给coverage，不声称运行固件时长。游标绑定车辆、方法、排序；source_last_at为全量最新start。

## 验证与交付

已获维护者确认：唯一业务测试缝HTTP/ASGI＋新建隔离PostgreSQL；不继承生产DSN。不mock SQL。按切片red→green并运行mypy/ruff，最终完整pytest及双轴审查（基线为上述HEAD）。交付0.4.0 OpenAPI、meta逐项能力、固定算例、DST/分页/权限/缓存/缺列与零值验证、730天大夹具EXPLAIN证据。只本地提交当前分支，不推送/部署，不关闭父spec与#28。原有README及交接文档内容保留。

## 源列与单位补充

| 输出 | 源列 | 单位/语义 |
| --- | --- | --- |
| soc_pct / usable_soc_pct | positions.battery_level / usable_battery_level | %，独立原值 |
| rated_range_km / ideal_range_km | positions.rated_battery_range_km / ideal_battery_range_km | km，独立原值 |
| inside_temp_c / outside_temp_c / driver_temp_setting_c | positions.inside_temp / outside_temp / driver_temp_setting | °C |
| tpms_fl/fr/rl/rr_bar | positions.tpms_pressure_fl/fr/rl/rr | bar，原始存储单位 |
| power_kw | positions.power | kW，有符号原值 |
| ascent_m / descent_m | drives.ascent / descent | m，非负有效 |
| distance_km / duration_min | drives.distance / duration_min | km / min，非负有限有效 |
| energy_added_kwh | charging_processes.charge_energy_added | kWh，非负有效 |
| start_soc / end_soc | charging_processes.start_battery_level / end_battery_level | %，0–100有效 |
| 固件履历 | updates.start_date / end_date / version | UTC记录时刻 / 原始版本文本 |

温度/胎压单位核对：[TeslaMate 官方集成字段说明](https://github.com/teslamate-org/teslamate/blob/main/website/docs/integrations/mqtt.md)。
字段类型以本项目4.3.0夹具投影和启动探针校验，不宣称验证所有TeslaMate安装版本。


## 本地交付记录（2026-10-29）

- 生命周期：Draft → Accepted → Implemented locally；实施 #43：Ready → In progress → Implemented locally。
- 维护者确认HTTP测试缝与本轮HEAD基线后实施；spec #42与实施 #43以GitHub原生父子关系关联，旧#29回链CH06承接。
- 最终完整回归：318 passed、13 deselected（默认排除slow），403.01秒；无失败或skip。
- 新增规模测试1项通过（包含2000固件/1000状态），既有统计规模3项通过；其余9项既有slow本轮未重跑。
- ruff / mypy（48个源文件）通过；Standards与Spec最终复核均无遗留发现。
- OpenAPI 0.4.0 SHA-256：64a5c297ed8c7b94cef72270d076246d5b5874a6809b87808c4072e0a2e993bd。
- 详细方法与SQL执行计划见 [验证记录](../testing/owner-experience.md)。原有README修改保留工作区，不并入本轮提交。
- 当前分支codex/statistics-insights，仅本地提交；父spec与#28继续开放，真实站点/Grafana与Mobile验收独立完成。
