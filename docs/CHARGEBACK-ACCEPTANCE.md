# 拒付账务与运营接口验收

范围：商业 PRD 的独立拒付记录、幂等冲正、差额冻结/追踪和运营查询/处置。这里的证据只来自隔离模拟与 PostgreSQL 测试，不代表选定或接通正式支付 SDK、真实拒付或生产上线。

## 事件契约

现有 `POST /billing/live/webhook` 新增规范化事件：

```json
{
  "merchant_id": "APPROVED_MERCHANT_ID",
  "event_id": "provider-event-id",
  "order_id": "gateway-order-id",
  "type": "payment.charged_back",
  "status": "charged_back",
  "provider_transaction_id": "original-payment-id",
  "provider_dispute_id": "stable-dispute-id",
  "payment_amount_minor": 100,
  "currency": "USD"
}
```

示例金额不是售卖价格。这个事件只能代表支付方已确认扣走的拒付本金；争议刚开启、预警、未核实的客户投诉、手续费和推测的最终损失不能映射为它。sidecar 必须先使用正式 SDK 验证原始通知，并核对所选支付渠道的实际扣款语义，再签署该规范化通知。当前仍没有选定/实现正式渠道映射。

网关验证原始报文的 HMAC、时间窗、nonce；本事件额外将**签名正文中的 merchant_id** 与配置商户绑定，并校验供应商、订单、币种、原交易号、正整数金额和争议号。非拒付事件不得携带争议号/拒付状态。原始支付正文不落库；沿用已有支付事件摘要与幂等记录，和模型素材/提示词禁止落库的规则分离。

## 账务处理

| 条件 | 动作 |
|---|---|
| 原订单已入账；拒付本金等于整笔购买金额；没有退款或其他拒付记录 | 依原订单冻结的服务额度冲正一次，不按当前汇率/售价重新算 |
| 可用额度足够 | 扣可用额度，对应 `PLATFORM_CLEARING`；记录 `recovered` |
| 可用额度不足 | 只扣现有可用额度，差额记入 `PLATFORM_RISK`；记录 `risk` |
| 存在模型预授权 | 保留 `reserved_microusd`，让原任务完成结算/释放，不把拒付当作未发生调用 |
| 部分/超额拒付、原支付未确认、退款重叠或同订单新争议号 | 保存独立 `pending_reconciliation` 记录，停止自动额度计算；零账本分录不等于零现金损失 |
| 同事件重放 / 同争议换新事件号 | 持久化事件幂等或争议唯一键识别；不再扣额度 |
| 同争议号换订单/金额 | 409 拒绝冲突，不覆盖旧证据 |
| 拒付完成后收到退款确认 | 保存待对账退款与拒付关联状态；不再冲正客户额度，也不自动再次请求退款 |

所有首次拒付记录均在同一事务冻结账户、撤销 Key/会话/恢复凭证。新模型预授权与拒付通过 User 行锁串行；金额只使用整数。钱包、独立拒付记录、追加型双式账本、事件幂等和 outbox 在同一提交可见，不修改旧账本。记录存在后禁止创建/重新认领退款命令；已经外发的退款仍可能完成，因此需要上述重叠对账路径。

`payment_chargebacks` 保存支付本金/币种、原服务额度快照、追回额、未处置差额、核销额、状态和原因。它是可更新的状态投影，不宣称状态表本身不可变；财务分录与人工操作审计仍由已有数据库追加型保护约束。服务额度 microUSD 账本不代替支付机构现金总账，拒付手续费没有被假设为零。

## 运营接口与权限

- `GET /ops/chargebacks?limit=50&offset=0`：`payments:read`，分页最大 200。
- `GET /ops/chargebacks/{id}`：`payments:read`。
- `POST /ops/chargebacks/{id}/risk-disposition`：`payments:risk:write`；`action`、至少 10 字符原因和稳定 `idempotency_key`。
- `action=recover_available`：全部模型占用已清空、可用额度足够时全额追回已确认差额。
- `action=write_off`：先追回当前可用额度，再把剩余差额明确记为 `PLATFORM_LOSS`，不是直接删除债务或向客户卡片扣款。

处置必须核对差额投影与账本一致，只接收 `risk`；`pending_reconciliation` 不能用这个接口直接核销。人工操作与账本同事务提交；审计写入失败则全部回滚。同命令只产生一次处置/审计，换动作复用同一命令返回 409。

处置不自动解冻、不恢复旧 Key。独立账户解冻接口会拒绝尚有拒付差额或待对账的账户；全部问题处理后仍需单独审核解冻。现有告警新增 `chargeback_risk`（critical），可进入邮件摘要；ack 只是签收，不是账务解决。真实通知投递/排班未因本改动启用。

## 验证

- `tests/test_chargebacks.py`：重复/乱序/金额冲突、HMAC/商户绑定、退款重叠、占用保留、处置幂等、权限、解冻门槛、审计失败回滚。
- `tests/test_ops_alerts.py`：拒付进告警，签收不关闭记录。
- `tests/test_migrations.py`：SQLite 真迁移、ORM 对齐、带拒付记录时禁止降级删除。
- `scripts/verify_chargebacks_postgres.py`：仅接收显式确认的本机 `kunlun_ci` 测试库；16 个并发事件只产生一笔冲正，拒付/managed 模型预授权竞争，以及已有占用释放后的审计追回。
- `scripts/ci_postgres_gate.sh`：新表 RLS/运行权限与迁移链纳入既有 PostgreSQL gate，末尾运行上述并发验证。

## 仍需完成，禁止当成已上线

1. 正式商户、渠道 SDK，以及从该渠道原生争议事件到确认本金扣款的映射和真实对账。
2. 争议开启/撤销/胜诉返还、费用及部分/重叠案例的受控对账闭环。本批只保留并阻断不确定案例，不提供猜测性自动解决。
3. 真实运营身份、排班与权限验收。后续 UI 批次已补齐拒付列表、详情、二次确认及处置交互；本地模拟浏览器验收仍不代表真实运营人员或生产入口通过验收。
4. 真实小额支付、调用、退款、异常演练和生产环境验收。

新 schema head 为 `0016_chargebacks`。本轮仅允许可丢弃测试库迁移；生产迁移须明确对象、备份与批准。不要把旧镜像直接指向新 schema，也不要为回滚删除拒付、事件或账本。优先前向修复；只有无任何拒付记录且获准维护时才能降回 `0015_key_policy`。旧运行代码不理解拒付状态，不能用解除冻结/重置余额绕过这一边界。

## 运营台 UI 后续交付

`/ops/console` 新增中英文拒付模块：分页与精确 ID 查询、现金最小单位/服务额度分栏、追回/差额/核销详情。拒付告警直接进入该模块。`payments:read` 只读；仅 `payments:risk:write` 且记录仍为 `risk`、金额可精确表示时提供处置动作。待对账、已追回和已处置记录不提供核销按钮。超过 JavaScript 安全整数范围的金额不用于执行确认，需受控工具核查。

确认框保留目标 ID、原订单、币种、观察到的金额、动作、原因与幂等操作号。中英文切换不改变已准备的命令；确认才写入一次；网络失败后保留命令并要求查原记录。钱包现状、模型占用和账本仍由服务端在事务内核验，不靠前台状态放行。

浏览器验证命令（仅 loopback 合成资料；没有真实支付或邮件）：先运行 `.venv/bin/python tests-browser/ops_fixture.py --chargebacks`，再使用既有 Playwright 环境执行 `node tests-browser/ops-chargebacks.cjs`。覆盖权限、待对账禁操作、金额精度、追回/核销、丢失响应、中英文确认与 390px 移动布局。原有 `tests-browser/ops.cjs` 必须在不带 `--chargebacks` 的新 fixture 上回归，避免测试数据互相干扰。

本 UI 批次没有新迁移。回滚界面时保留 `0016_chargebacks` 和账务保护；不要删除历史拒付记录或账本。截图只用于合成页面验收，不得对外冒充真实客户/拒付案例。
