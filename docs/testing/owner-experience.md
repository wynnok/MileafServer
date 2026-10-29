# 车主体验接口本地验证

日期：2026-10-29。来源 spec #42、impl #43，规格见
[owner-experience-spec.md](../product/owner-experience-spec.md)。只支持本地合成数据验证，
真实TeslaMate/Grafana对账仍由 #28 承担。

## 环境与测试缝

唯一业务测试缝是维护者确认的HTTP/ASGI＋新建隔离PostgreSQL。
沿用 tests/conftest.py 的预拉取postgres:16-alpine、每用例独立数据库与只读角色，
连接配置由夹具直接构造，不读取生产DSN，不对生产做DDL/写入/权限修改。
最初Docker未运行导致测试跳过；维护者恢复Docker后，以下验证实际执行，无跳过。

所有命令在项目根目录执行，uv缓存放在 `.cache/uv`，未全局安装依赖或修改宿主配置。

```sh
UV_CACHE_DIR=.cache/uv uv run pytest tests/test_owner_experience.py -ra
UV_CACHE_DIR=.cache/uv uv run pytest tests/test_owner_experience.py -m slow -s -ra
UV_CACHE_DIR=.cache/uv uv run pytest tests/test_statistics_performance.py -m slow -ra
UV_CACHE_DIR=.cache/uv uv run ruff check src tests
UV_CACHE_DIR=.cache/uv uv run mypy
UV_CACHE_DIR=.cache/uv uv run python scripts/freeze_openapi.py
UV_CACHE_DIR=.cache/uv uv run pytest -ra
```

测试包括真实0/null、温度负值、同时间位置点去重、原始gap、输出点数限制、跨期事件、
online/asleep/offline状态值、ongoing、查询绑定分页、跨页固件全量前驱、地点下钻匹配集、
SOC100%末箱、非负样本分位数、DST重复小时合并、原始负功率、速度按时间加权、
字段缺失/类型漂移、Bearer/车辆404、条件响应、缓存命中前资源复核及OpenAPI具体DTO引用。

## 查询预算与执行计划

大夹具：20,000行程、200,000位置点、5,000充电会话及100,000充电采样，额外加入
2,000固件记录和1,000状态区间。地点与习惯查询使用730天范围，历史/时间线使用各自较小上限。
夹具数据并非模拟所有真实车况密度，不将耗时外推为生产SLO。

- 关闭应用缓存后，8个新增车辆端点逐一HTTP调用均小于5秒、items≤1000。
- 最终采集运行的HTTP耗时包含EXPLAIN额外开销：battery 135ms、environment 125ms、
  timeline 17ms、locations 104ms、areas 138ms、habits 57ms、software 19ms、latest 67ms。
- [17条真实SQL执行计划](owner-query-plans.json) 保存实际SQL、EXPLAIN (ANALYZE, BUFFERS,
  FORMAT JSON)原始结果；最大单条Execution Time为40.797ms。采集通过临时透明查询记录器，
  每次先执行真实只读查询，再在同一隔离库执行同参数EXPLAIN，不替换结果或mock SQL。
  临时记录器已清理，不作为应用依赖交付。
- 业务查询数（不含车辆查找与启动探针）：history 3、timeline 1、latest 1、locations 2、
  areas 2、habits 4、software 1、speed-histogram 2（含行程存在性检查）。
- 受共享参数列表扩展影响的旧usage/conditions SQL已改为动态参数序号；22项功能回归、
  3项既有统计规模回归通过。没有放宽5秒SQL超时、添加TeslaMate索引或使用生产数据。

## 审查

Standards初审2项硬性问题与1项缓存性能建议；Spec初审4项、复核1项资源缓存问题。
OpenAPI序列化schema、可选爬升类型兼容、时间线实际状态、能力依赖及缓存检查顺序
均已修复并复核通过，无遗留发现。审查基线为86bf3111606aca9277e26a3024098c29b2c8f152。
最终完整回归结果与生命周期状态在规格末尾记录。


最终完整回归：**318 passed、13 deselected，403.01秒，无skip/失败**。
另行选择执行的新增规模测试1项、既有统计规模3项均通过。其余既有slow本轮未重跑。
