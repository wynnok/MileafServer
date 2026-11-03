# ADR-0004: 有限充电费用补录

- 状态：Accepted
- 日期：2026-11-03
- 替代：ADR-0002 第 1、2、4 条中绝对只读的约束；其他约束保留。

## 决定

Mileaf Server 定位为默认只读、允许显式启用有限写入的 TeslaMate 适配层。
唯一业务写入是补录已结束且 cost 为 NULL 的充电会话费用，只更新 charging_processes.cost。
不新增表、列或索引，不读取 Tesla 凭据，不开放车辆控制、批量写入或已有费用覆盖。
0 元表示已知免费，不属于缺失费用。

`PATCH /v1/charging-sessions/{session_id}/cost` 使用现有 Bearer 鉴权。
请求：`{"unit_price":"1.250000"}`，单价非负、最多 6 位小数、最大 999999。
响应包含 session_id、unit_price、billing_energy_kwh、cost，十进制数以字符串返回。
单价货币单位沿用用户账本，不做币种转换；单价仅用于此次计算，不持久保存。

按 TeslaMate ChargeLive.Cost 的 per_kwh 语义独立实现：取 charge_energy_added 与
charge_energy_used 的较大可用值，乘以单价，总费用四舍五入为两位小数。
电量缺失、非正数、非有限值、达到 1,000,000 kWh 或未结束的会话拒绝补录；上限匹配当前 numeric(8,2) 电量列，防止费用溢出。
来源：https://github.com/teslamate-org/teslamate/blob/main/elixir/lib/teslamate_web/live/charge_live/cost.ex

未启用返回 403；记录不存在返回 404；已有费用或无法计费返回 409；
参数错误返回 400/422；缺少数据库授权返回 503。并发通过原子条件更新保证仅一次成功。
重复请求返回 409，客户端应重新获取详情确认结果，不能盲目重试覆盖。
成功后清空当前进程缓存；其他实例和客户端缓存按既有 TTL 到期，客户端应主动刷新。

## 部署与回退

默认关闭。管理员为现有专用角色增加唯一列权限（按实际角色名替换）：

```sql
GRANT UPDATE (cost) ON public.charging_processes TO mileaf_reader;
```

设置 `MILEAF_CHARGING_COST_WRITES_ENABLED=true` 后重启服务。无需新增网络端口。
普通查询仍使用只读事务，仅此接口显式开启读写事务。
不得授予表级 UPDATE、INSERT、DELETE 或凭据访问权限。
数据库授权限制可写列，记录条件由服务端保证；持有数据库凭据者不受 API 条件约束。

回退：关闭配置并重启，撤销列权限：

```sql
REVOKE UPDATE (cost) ON public.charging_processes FROM mileaf_reader;
```

回退不会删除已补录费用。此变更不自动修改用户部署或生产数据库权限。
