# 受控告警邮件摘要：实现和交付边界

本批为商业 PRD P0-10/P0-12 增加可外部调度的一次性通知命令、持久化摘要、并发发送认领、SMTP 传输与运营投递记录。没有开通或修改真实邮件账户、收件人、调度器、生产资源，没有发送真实通知。完整无人值守运维仍须部署、心跳监测和值班/收件箱验收。

## 默认不发送

```bash
# 只预览规则数量；不写通知记录、不发送邮件、不播种价格或迁移数据库
.venv/bin/python -m scripts.alert_notifications
```

需要既有受控 `KUNLUN_ENV=production`、`managed_gateway`、`KUNLUN_VAULT_BACKEND=supabase_vault` 配置，即使预览也执行 production 模式的配置安全校验（含 TLS、数据库角色及同 Supabase 项目约束）。首次真实邮件验收应使用独立、获批准的资源，不因环境名是 production 就假定资源已经上线。命令另验证 schema head、runtime 角色权限和平台 Vault 函数边界；失败时返回非零，输出不含凭据。预览也要读取内部数据库/Vault 元数据，应在获授权环境运行；不是匿名公网健康检查。

真实发送另须由负责人批准**精确环境、收件人和邮件账户**，随后由运维通过密钥系统配置：

- `KUNLUN_ALERT_NOTIFICATIONS_ENABLED=true`（精确值；默认 false）。
- `KUNLUN_ALERT_RECIPIENT`：一个普通 ASCII 邮箱地址；不支持多收件人、显示名、换行、国际化邮箱。
- 既有 `KUNLUN_SMTP_URL`、`KUNLUN_EMAIL_FROM`、`KUNLUN_PUBLIC_BASE_URL`；SMTP 凭据不进入网页、仓库或通知记录。
- 正确的运行库与独立 Vault executor 连接；完整生产预检仍需独立执行，不用通知脚本代替它。

只有显式执行 `python -m scripts.alert_notifications --send` 且发送开关开启才会排队并尝试发送。网页启动、普通维护命令和当前 Vercel Cron 不会因此自动发信。没有网页发信/改收件人按钮。

外部调度可在授权部署后按 5 分钟一次配置，每次进程只运行一个 tick，最多认领并尝试一封。**本仓库未安装真实调度任务。** 必须监测计划任务是否实际启动、退出状态和投递记录；进程没有被调度时无法自己报告“自己没运行”。

## 摘要与重复控制

复用 `outbox_events`，专用 `topic=ops.alert.digest`；不消费、修改或删除支付的其他 outbox 事件。没有 schema 迁移，仍为 `0015_key_policy`。

同一配置收件人、同一 **UTC 观察小时、同一最高严重级别**，使用确定性 UUID 建立最多一条摘要。warning→critical 升级可在同小时另建一条；下一小时仍有告警时可以产生新的提醒。操作人员确认告警不抑制摘要，也不表示事故解除。

只保存和发送：原观察时间、通知 ID、规则 ID、严重级别和计数。通知正文不含客户邮箱、客户 ID、请求 ID、提示词、回答、来源异常文本、供应商密钥或运维凭证。收件地址只在执行配置/SMTP 内存中使用，数据库存摘要绑定；运营查询不返回该摘要绑定。

数据库原子认领 `pending → sending` 并提交后才调用 SMTP，不在数据库事务内等待网络。多个实例排队只得到一个 ID；多个发送者只能有一个认领成功。邮件 `Message-ID` 稳定，但不依赖邮件服务器按该 ID 去重，也不承诺端到端 exactly-once。

每次 tick 可继续发送该收件人最早的未尝试积压摘要。因此“每观察小时最多两条”**不是实际投递小时最多两封**；恢复积压时会发送较早摘要，每次仍只尝试一封，邮件明确保留原观察时间。修改收件人后不会把旧地址的队列自动转给新地址。地址大小写也属于配置绑定，修改须审慎。

## 状态与失败语义

| 状态 | 含义 | 自动动作 |
|---|---|---|
| `pending` | 尚未获得发送认领 | 后续 tick 可恢复并发送 |
| `sending` | 已提交认领，可能正在发送或进程已停止 | 不再认领；超过 5 分钟在查询中显示为 unconfirmed |
| `accepted` | SMTP 调用正常完成且未返回拒收地址 | 不重发；不代表进入收件箱、被阅读或有人响应 |
| `unconfirmed` | SMTP 异常/拒收，或认领后未能确认结果 | 不自动重发，不冒充“确定没发出” |

若 SMTP 已接受但数据库结算失败，命令返回错误，记录可能保留 sending；查询超时后呈现 unconfirmed。不得通过把状态改回 pending 来盲目重发。未尝试项可自动恢复；**已尝试的不确定项必须人工核查 SMTP 日志和实际收件箱**。当前没有将人工核查结果写回为 delivered 的接口，不伪造送达回执。

后续小时的新提醒可能重复描述同一仍存在的条件，这不等于重放旧邮件。历史未确认记录保留；源告警解除不证明历史邮件送达。当前 tick 无活跃规则且无未尝试项可返回 `no_active_alerts`，不能据此推断所有历史记录已送达。

命令输出中的 `inbox_delivery_verified` 永远为 false。accepted/no_active_alerts/仍在 5 分钟认领窗口中的 sending 返回 0；当前摘要未确认、配置/数据库/传输异常返回非零。需结合记录观察，不把退出 0 当作全站健康。

## 运营查询

`GET /ops/notifications?limit=50&offset=0` 要求 `alerts:read`，按时间分页，最多每页 200。只返回本模块记录、原观察时间、状态、认领时间和有限规则摘要。客户凭证被拒绝；`accepted` 明确不是 inbox delivery。

`/ops/console` 新增“Notification records / 通知投递记录”只读模块，复用现有界面，无额外写操作。原始元数据在展开区；不知道某条状态的原因时，先核对记录与调度日志，不编辑钱包或队列伪造成功。

## 本地证据

专项测试覆盖摘要去重、级别升级/下一小时提醒、空条件无队列、收件人校验与绑定、脱敏、并发认领、先提交再 SMTP、未知结果不重发、旧 pending 恢复、崩溃认领状态、权限、TLS/稳定 Message-ID、SMTP 拒收、默认预览零写和显式发送门槛。传输全部注入模拟对象，没有真实 SMTP 发送。

本地全量 461 项 Python 测试、48 项前端测试通过，分支覆盖率 83%；通知专项 9 项，连同既有身份邮件测试共 21 项通过。另保留既有一条 Starlette/httpx 弃用警告，不扩大依赖升级范围。

隔离 PostgreSQL 16 的 runtime 角色实测：16 个并发排队者得到一个事件，16 个发送者只产生一次模拟 SMTP 调用；发送期间可从另一个连接读到已提交的 sending 状态。既有角色/Vault/价格/预算/Key/审计并发回归继续通过，假 Vault 不代替生产 Vault 验收。

浏览器回归验证通知列表只读、accepted 说明可见、查看不产生 POST，并保留告警确认、价格、账户冻结、模型对账、模拟退款及移动端回归。截图在忽略目录 `release-artifacts/ops-notification-desktop.png`。

```bash
.venv/bin/python -m pytest tests/test_alert_notifications.py tests/test_identity_production.py -q
.venv/bin/python -m coverage run --branch -m pytest -q
.venv/bin/python -m coverage report --fail-under=80
npm run check
# 仅 loopback 测试夹具和现有 Playwright
.venv/bin/python tests-browser/ops_fixture.py
node tests-browser/ops.cjs
# 仅获明确确认的 kunlun-ci-disposable 测试数据库
bash scripts/ci_postgres_gate.sh
```

## 回滚与未完成

停止外部调度并关闭发送开关后再回滚该 worker；在途 SMTP 可能已发送，保留 outbox 状态，不重置认领、不删除记录。API/页面可回退，保留其他已完成的安全修复。身份邮件共享 SMTP 发送函数，已运行既有身份测试；拒收响应现在按失败处理。

仍待真实 SMTP 账户/收件人批准、调度与心跳、SPF/DKIM/DMARC/收件箱验证、值班升级及人工不确定结果闭环。本批不是事故生命周期/送达回执平台，也不是整个商业站完成；正式支付、供给资格、受保护部署、真实小额交易和恢复/容量验收仍待完成。
