# 统计洞察合成对账与接入输入

本页提供隔离夹具可复现的输入和独立手算，不代表真实 Grafana、生产数据或真机验收。
权威契约为 GitHub #34；字段来源、单位与方法定义见 [API 说明](../api/v1-surface.md)。

## 复现环境

在仓库根使用项目本地 `.venv` 与缓存，运行：

```sh
UV_CACHE_DIR=.cache/uv uv run pytest tests/test_driving_coverage.py tests/test_driving_analysis.py tests/test_driving_conditions.py tests/test_range_observations.py
```

夹具仅在新建本地 PostgreSQL 数据库内装载，每测试隔离，禁止继承生产 DSN。
Docker 镜像或数据库不可用导致 skip 时，不算验收通过。
停车观察的合成输入和全部性能结果由最终集成验收记录，本页不提前声称通过。

## 驾驶与温度独立算例

`test_driving_conditions.py::test_conditions_pairing_and_evidence` 调用同一隔离种子：

| id | 距离 km | 记录时长 min | 估算净能量 kWh | 均温 °C |
|---|---:|---:|---:|---:|
|701|5|30|1.5|10|
|702|10|30|3|10|
|703|60|60|15|10|
|704|0|10|null|10|
|705|-3|20|4.5|10|
|706|null|45|7.5|10|
|707|100|null|22.5|10|
|708|进行中|—|不输出最终值|10|
|709|20|0|3|10|
|710|null|-5|3|10|

范围：2025-06-01T00:00:00Z（含）到2025-07-01T00:00:00Z（不含），vehicle=1、tz=UTC。
车辆 efficiency=0.15 kWh/km、preferred_range=rated；输入能量由正的额定续航差形成。

- 完成候选9趟、活跃7日；有效距离195km，已知原始距离包含负值的和为192km。
- 有效记录时长195min；平均速度有效集距离75km、时长130min，速度4500/130km/h。
- 距离分位数 P50=15km、P90=80km；时长P50=30min、P90=51min。
- 电耗配对子集701/702/703/707/709，5趟、195km、45kWh，电耗45000/195 Wh/km。
- 温度[10,15)组9趟，配对数量和里程均达首版门槛，sample_status=adequate。
- 0km不入电耗分母；负/缺距离不进距离组；duration=0不进速度分母；进行中不进统计。
- evidence_filter交给行程列表，完整 matched_drives=9；配对5趟仅是该组有效子集。
- 将701距离改为5.004后，有效距离195.004、已知原始距离192.004；新增字段不展示舍入。

ST02 的基线算例独立于以上场景：2026-03-02到03-06共有3趟完成行程，
距离0/20/20km，能量3.75/null/3.75kWh。旧净能量7.5、旧净电耗188Wh/km；
新配对能量3.75、距离20、电耗187.5Wh/km，eligible=1、excluded=2。
这说明旧字段与新增配对字段不能互换。

## Mobile 两次 stats 比较请求

以本地时区Asia/Shanghai的本月同进度为例，分别读取同车：

```text
GET /v1/vehicles/1/stats?kind=driving&bucket=day&tz=Asia%2FShanghai&from=2026-10-01T00:00:00%2B08:00&to=2026-10-28T19:10:00%2B08:00
GET /v1/vehicles/1/stats?kind=driving&bucket=day&tz=Asia%2FShanghai&from=2026-09-01T00:00:00%2B08:00&to=2026-09-28T19:10:00%2B08:00
```

服务器会按请求时刻裁剪未来to；客户端须核对响应中的有效query和各自as_of，
不能只看请求值或假定两次原子快照。同method/version/range_kind且两期adequate、
日历与进度对齐时，展示配对值绝对差current-reference，以及基期非零时的相对差。
基期0、缺历史、DST重复/不存在时刻、闰日/月末不足均须按契约显示原因；
不由手机重建能量、平均桶电耗或从已载入页估计全量统计。

## 续航权重的手算输入

构造完成会话A：同一结束日100个有效点均为SOC50、rated200km，点值均400km；
同日会话B：1个点SOC50、rated300km，点值600km。该日值是(400+600)/2=500km，
而不是按101个点加权。第二有效日1会话的值为300km，第三日值为700km。
该月日值[300,500,700]给P50=500、P10=340、P90=660km（percentile_cont）。
若只有前两日，则P50=400km，但sample_status=sparse；缺月null、不插值。
此输入可按既有 `observation_session` 测试fixture逐会话装载，窗口按会话ended_at归期，
SOC/rated必须同点，usable/ideal不参与；不是容量或SOH模型。

## 方法与阈值状态

- paired-net-consumption-v1：同批距离>0且现有估算能量有效。
- paired-comparison-quality-v1：每期至少5趟配对、有效距离覆盖至少80%。
- usage-distribution-v1：固定0/10/50km距离边界、ISO星期1..7、percentile_cont。
- temperature-association-v1：5°C半开组，adequate需配对≥5趟且≥50km。
- session-day-rated-range-v1：SOC20..90闭区间、额定续航>0、月度至少3有效日。
- closed-window-soc-v1：精确可靠端点、时长≥1h、绝对里程差<1km；状态重叠需排除。

所有阈值validation=provisional，是首版产品筛选规则，不是科学显著性或电池检测标准。
#28真实Grafana对账、生产版本覆盖和Mobile真机验收保持开放；本地实现和合成测试通过
不等同于这些真实验收完成。最终#41还需记录全量pytest/slow、ruff、mypy、双轴审查，
以及至少10000行程/5000会话/100000采样及跨期状态下各端点预算与运行环境。

## 本地综合验证记录（2026-10-28）

验证分支为 `codex/statistics-insights`，范围为 #35–#41；末次有效集修正整合提交
`332614d`。环境为 macOS Darwin 27.2.0 arm64、uv 0.13.6、Docker Engine 29.8.2、
预拉取的 PostgreSQL 16 Alpine 容器。全部业务数据由 function 隔离数据库合成，
不读取生产 DSN、生产数据或 Tesla 凭据，不修改生产结构或索引。

执行命令均从项目根目录运行，缓存限于 `.cache/uv`：

```sh
UV_CACHE_DIR=.cache/uv uv run pytest
UV_CACHE_DIR=.cache/uv uv run pytest -m slow -ra
UV_CACHE_DIR=.cache/uv uv run pytest -m slow tests/test_statistics_performance.py -ra
UV_CACHE_DIR=.cache/uv uv run pytest tests/test_driving_analysis.py tests/test_driving_conditions.py -ra
UV_CACHE_DIR=.cache/uv uv run ruff check src tests
UV_CACHE_DIR=.cache/uv uv run mypy
UV_CACHE_DIR=.cache/uv uv run python scripts/freeze_openapi.py
git diff --exit-code docs/api/openapi-0.3.0.json
```

- 原始停车查询的大夹具回归曾返回 `503 upstream_timeout`；红测试提交 `603aa3e`。
  后续将结束位置点批量匹配、窗口/状态质量及完整集聚合迁入 SQL，并有界分页证据；
  禁止以放宽 5 秒 SQL 超时掩盖问题。
- 大夹具含 20,000 行程、200,000 位置点、5,000 充电会话、100,000 充电采样，
  超过规格要求的 10,000/5,000/100,000。状态覆盖另由合成跨期、重叠及开放状态案例验证；
  不声称大夹具模拟了所有真实状态密度。
- 全部既有 slow：10 passed、288 deselected，97.07 秒。此轮启动时尚未加入两个
  driving-analysis 大夹具参数例；后者单独执行。无 skip。
- 同轮旧端点观测：行程列表 p95 128.79ms/300ms、行程序列 max 101.55ms/600ms、
  月驾驶统计 max 103.65ms/800ms、首页 max 149.61ms/400ms。关闭应用缓存，
  不包含容器初始化和造数时间；这些仅是本机回归信号，不是生产 SLO。
- 末次有效集负值修正后，usage/conditions 相关 HTTP 回归：22 passed，41.21 秒。
  候选保留原值，有效距离/时长单独排除负值；未重复执行无关旧接口测试。
- `ruff check src tests` 通过；mypy 通过 41 个源文件；重生成 OpenAPI 后无差异。
- Standards/Spec 独立审查发现的停车状态重叠、空窗口状态、SQL 聚合/性能、
  新增数值舍入、conditions 嵌套 as_of 导致 304 失效及逐有效集覆盖问题均有回归修正。
  Standards 轴未发现额外的独立硬性违规。
- 部分子代理 worktree 由其他活动任务共享，应用归档保护返回
  `Another active task still uses this worktree`；保留这些工作树，不强制删除共享资源。

- 完整普通回归：288 passed、10 deselected，427.11 秒，无 skip/失败。该轮在
  `5714acd` 启动；随后唯一有效集负值修正 `9e9d274` 合入 `332614d` 后，
  相关 22 项 HTTP 回归再次通过，补足最后差异验证。
- 最新大夹具 usage/conditions/parking 回归：3 passed，30.84 秒；每个请求均 <5 秒，
  停车从原始超时转为通过。与上述全 slow 合并覆盖 12 个不同 slow 用例，
  parking 在两轮中重复验证；无跳过。续航 slow 验证有效点 >50000、会话 >2000、
  固定 12 月桶且无原始样本数组。
真实 Grafana 对账、真实 TeslaMate 安装版本覆盖和 Mobile 真机验收仍开放；
本记录只支持 Implemented locally，不代表推送、远程合并或部署。
