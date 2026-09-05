# 运营告警聚合与确认记录

本批实现商业 PRD P0-10 的数据库告警聚合、独立权限和审计确认，并加入 `/ops/console`。没有发送真实邮件/短信/聊天通知，没有完成完整通知送达闭环，也不改变真实收款和生产发布门槛。

## 已实现规则

| 规则 ID | 触发条件 | 级别 / 处理入口 |
|---|---|---|
| `model_reconciliation` | 商业模式请求处于 `pending_reconciliation` | warning / 模型对账 |
| `stale_reservations` | 商业模式 `reserved` 超过配置的占用租约（含边界） | critical / 模型对账；先核查维护任务，必要时在批准环境运行既有维护，将超时项转待对账；不得自动释放 |
| `payment_reconciliation` | 订单待对账，或 checkout 创建租约超过 5 分钟/缺少租约时间 | warning / 订单 |
| `refund_reconciliation` | 退款待对账，或 requesting/retrying 租约超过 5 分钟 | warning / 订单；正常在途退款不立即告警 |
| `payment_risk` | 订单存在 `risk_reason` | warning / 订单；不是对所有真实支付拒付类型的支持证明 |
| `refund_risk` | 退款状态 `risk` | critical / 订单；不能通过确认告警核销亏损 |
| `platform_budget` | 已确认成本 + 占用达到有效 UTC 日预算 80% | warning；达到/超过 100% 为 critical / 预算 |
| `supply_observation_failed` | 无法读取平台 Vault 渠道元数据 | critical / 渠道；状态未知，不宣称渠道已停用 |
| `supply_unavailable` | 已上架且在配置目录中的模型没有启用的配置渠道 | critical / 渠道；只观察元数据，不读取 Key、不发计费探针 |
| `price_below_supply` | 已上架模型售价低于任一配置供应商单价 | critical / 售价；检查成本目录与实际账单后决定调价或下架 |

预算告警不替代已经存在的预算硬阻断；确认记录不能打开熔断。售价告警也不自动改价、下架或证明实际净毛利。正常的“客户还没付钱”订单不因等待时长自动成为失败或重复支付请求。

## 接口与权限

- `GET /ops/alerts`：`alerts:read`；最多返回上表 10 类当前触发的汇总，严重项优先。含观察时间、规则 ID、计数、有限元数据、观察修订号、关联模块和相同汇总的既有确认记录。无分页、无模型正文、无客户邮箱/密钥/收银 URL。
- `GET /ops/alerts/{kind}`：`alerts:read`；重新计算对应当前汇总，不存在或条件已解除返回 404。不能把 404 单独当作全站健康证明。
- `POST /ops/alerts/{kind}/ack`：`alerts:write`，另有入口/短时令牌保护；仅写追加审计。

确认请求：

```json
{
  "expected_revision": "00000000000000000000000000000000",
  "operation_id": "unique-operator-receipt-001",
  "reason": "Reviewed aggregate observation, follow-up tracked separately"
}
```

修订号必须从当前 GET 取得，示例值不能用来确认真实观察。服务端重新计算；修订过期/条件不再触发返回 409。相同操作号重试/并发只有一条审计，其余返回 409，客户端先核对原操作。成功响应仍为 `status=attention`，不是 `resolved`。

审计使用已有 `OperatorAction`：`target_type=ops_alert`、规则 ID、主体、权限、原因、操作号、观察修订号。生产 PostgreSQL 已有追加型审计保护；本批没有新表或迁移，schema 仍为 `0015_key_policy`。

## 观察与确认的边界

规则查询来自共享数据库和平台 Vault 元数据，不是单个应用实例的内存计数。查询失败返回脱敏 503；Vault 元数据失败产生“状态未知”严重告警，不返回假健康空列表。没有探测模型、支付或邮件服务的实时可达性。

修订号仅对**汇总元数据**计算，不是提示词/回答哈希。计数、最早/最新时间、一个确定性样本记录 ID 等用来标识这次汇总。它不穷举每一笔事件，不是持久化事故 ID、独占认领或逐笔核验清单。相同汇总以后再次出现时可能显示过去的确认时间；确认记录不隐藏告警、不抑制通知、不证明本次事故已有人处理。

读取和确认之间业务状态可继续变化。确认记录只证明操作者接受了当时读取的观察，不锁定所有订单/请求/预算，也不阻止新故障。刷新会重新评估；实际处置须在有对应权限的订单、模型、渠道等模块核对对象后执行。

界面英文默认、中文切换；按严重程度突出，展示数量、观察时间与确认时间，关联模块按钮按实际 scope 开放。操作沿用“核查 → 确认精确命令 → 执行一次”，切换语言不改变命令或执行写请求。额外修复了重新查询后操作下拉框的空默认值。

## 验收与复现

本地 12 项告警专项测试通过：权限隔离、健康模拟供给零模型外呼、Vault/数据库故障不伪装成功、预算 80%/100%、过期确认、确认后资金占用不变、超时预授权、支付/退款租约和风险、严格原因/操作号校验。

最终本地全量 452 项 Python 测试通过，分支覆盖率 83%；48 项前端测试及 TypeScript 类型检查通过。保留一条既有 Starlette/httpx 弃用警告，未为此扩大依赖升级范围。

隔离 PostgreSQL 16 测试调用真实确认 handler，两个相同操作号并发得到 201/409；只写一条审计，钱包不变、告警继续存在。该证明不包含真实 Vault 加密或生产身份系统。

浏览器在可丢弃 loopback 夹具验证只读不可确认、确认前零写、双语命令保持、确认后待对账数量仍为 2、关联模块跳转、移动端不横向溢出，并回归原调价/冻结/模型对账/模拟退款丢响应流程。截图在忽略目录 `release-artifacts/ops-alert-desktop.png`、`ops-alert-mobile-zh.png`，均为模拟验收页面。

```bash
.venv/bin/python -m pytest tests/test_ops_alerts.py -q
.venv/bin/python -m coverage run --branch -m pytest -q
.venv/bin/python -m coverage report --fail-under=80
npm run check
# 另开终端启动本地夹具，再使用现有 Playwright
.venv/bin/python tests-browser/ops_fixture.py
node tests-browser/ops.cjs
# 仅明确标记 kunlun-ci-disposable 的本机隔离测试库
bash scripts/ci_postgres_gate.sh
```

## 未完成、运营与回滚

目前由获授权运营者手动刷新；页面退出后不会继续监控。外部告警投递、持久化事故周期/升级/提醒、未送达检测和值班响应尚待实现与真实渠道验收。空列表只代表所评估规则当时未触发，不能替代 readiness、账本完整性核对、持续容量/恢复演练或供给资格验证。

回滚界面/API 前停止新确认动作，保留所有 `alert_ack` 审计，移除不再使用的 scope 签发配置但不要通过改账回滚。其他功能的回滚仍须保留此前价格播种/锁及 frozen Key 安全修复。生产发布、真实通知目标/凭据及外部动作均需精确授权。
