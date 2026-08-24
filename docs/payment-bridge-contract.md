# 正式支付 HTTPS Bridge 契约

`app.services.live_payments.LivePaymentBridge` 是昆仑网关与支付机构官方 SDK sidecar 之间的 provider-neutral 协议层。网关不保存支付正文，不打印密钥；正式 SDK、商户证书和支付机构域名由独立 sidecar 持有。

## 签名

所有网关到 sidecar 的请求和 sidecar 到网关的响应，均使用以下请求头：

- `X-Kunlun-Merchant`
- `X-Kunlun-Timestamp`：Unix 秒时间戳
- `X-Kunlun-Nonce`：每次请求/回调唯一随机值
- `X-Kunlun-Signature`

签名原文为 `timestamp + "." + nonce + "." + 原始 HTTP body`，使用双方配置的 HMAC-SHA256 密钥，输出小写十六进制。默认允许时间偏差 300 秒；时间戳、nonce、签名缺失或不匹配必须拒绝，不能按未签名内容继续处理。

## 接口

sidecar 提供 HTTPS：

```text
POST /v1/payments/checkout
POST /v1/payments/query
POST /v1/payments/refund
POST /v1/payments/reconcile
```

现金支付金额统一为非负溢出安全范围内的整数 `payment_amount_minor`（例如 CNY 分），币种为三位大写 ISO 代码；它与网关内部服务额度 `credit_amount_microusd` 是两个不同单位，不能互换或由 sidecar 自行换算。订单号和供应商交易号只允许 ASCII 标识符。checkout/query/refund/reconcile 的响应必须回传并匹配 `order_id`、`payment_amount_minor`、`currency`、`provider_transaction_id`，否则 fail-closed；checkout 还必须回传 HTTPS `checkout_url`，refund 必须回传 `provider_refund_id`。checkout 与退款请求都必须携带网关已校验的 `idempotency_key`；sidecar 必须持久化该键，并将其透传给支付机构或建立同等强度的商户订单幂等映射。checkout 的客户端 `return_url` 只能与网关配置的公开站点同源。

## Webhook

sidecar 将原始 JSON 回调转发给网关，由 `verify_webhook(raw_body, headers)` 完成签名和时间窗校验。至少包含：`event_id`、`type`、`order_id`、`payment_amount_minor`、`currency`、`provider_transaction_id`、`status`；退款事件还应包含 `provider_refund_id` 并由业务域层完成额度冲正。允许事件类型：`payment.pending`、`payment.succeeded`、`payment.failed`、`payment.closed`、`payment.refunded`。同一订单的现金金额、币种、供应商交易号必须与本地订单一致，不能信任回调自行改变报价。`payment.closed` 只关闭未支付订单并释放未关闭订单名额，不入账服务额度。

网关返回规范化结果和 `idempotency_key=payment:<event_id>`。sidecar/上层必须把 `event_id` 和 nonce 做持久化幂等；同一个 nonce 或 event_id 对应不同正文必须拒绝。当前类内存集合只用于防止单进程重放，不能替代生产数据库中的 `payment_webhook_events` 唯一约束。

## 失败语义

网络异常、HTTP 失败状态、超大响应、无效 JSON、签名错误、时间窗错误、金额/订单/交易号不匹配都抛出已脱敏的 `PaymentBridgeError`。异常不包含上游正文、请求 body、密钥或底层错误文本；调用方不得在不确定的支付状态下自动重复扣款，应进入 query/reconcile 队列。网关先把 checkout 原子切换为 `checkout_requesting` 并持久化租约，只有取得租约的调用方可以接触 sidecar；新鲜租约的并发请求返回 409，过期租约转 `pending_reconciliation`，不得自动重建支付意图。认证的成功 webhook 可以在 checkout HTTP 响应返回前直接完成入账，pending 回调抢跑则进入对账。

网关在调用退款 sidecar 前先持久化 `requesting` 退款命令并把订单原子切换为 `refunding`；不确定结果转为 `pending_reconciliation`。当前产品只支持全额退款，数据库强制每订单最多一条退款记录。只有完全相同的退款幂等键可在五分钟租约过期后原子重领，或由认证退款 webhook 完成同一预留命令；不同键、并发命令或变化的 `provider_refund_id` 必须拒绝。同一用户不同订单并发退款会通过钱包行锁串行完成，不能让两笔账本冲正建立在同一个旧余额快照上。

如果供应商已退回全部现金而客户可用服务额度不足，网关扣除 `min(available, credit)`，以完整 `PLATFORM_CLEARING` 冲正和剩余 `PLATFORM_RISK` 差额保持账本平衡，同时冻结账户、吊销凭据。存在 `risk` 退款记录时禁止解除冻结；先清空在途预授权，再由 `payments:risk:write` 受控接口全额追回，或先追回现有额度并把剩余差额记为 `PLATFORM_LOSS`。处置、账本和带退款目标的运维审计在同一事务提交，且不会自动解冻账户。

主应用已接入订单、数据库 webhook 幂等、短时 scoped 运维权限、退款冲正与人工 query/reconcile 接口；生产环境仍需部署支付机构官方 SDK/certificate sidecar，并把人工接口接入经审批的定时日对账任务。本契约本身不构成支付机构接入、商户资质或生产合规证明。真实支付前必须做小额支付、退款、退款超时后同键重试、重复回调、失败回调和对账演练，并保留脱敏证据。
