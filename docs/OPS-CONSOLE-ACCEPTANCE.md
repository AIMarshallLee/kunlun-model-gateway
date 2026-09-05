# 独立运营台：功能与验收边界

本批推进商业中转 PRD P0-10，不改变公共商业网关目标。入口 `/ops/console`。不是生产发布声明，也不把已经存在的接口或模拟退款当成真实商户闭环。

## 已实现

英文默认、中文切换的独立运营界面，包含客户与 Key、订单与退款、模型待对账、渠道状态、站点日预算和审计查询。客户/订单/待对账/审计按页读取，支持精确客户、订单和逻辑任务 ID 查询。优先展示对象、状态、额度和风险摘要；原始元数据在展开区，均不展示模型正文或供应商 Key。

| 权限 | 界面及接口 |
|---|---|
| `console:read` | 新增 `GET /ops/session`：服务器核验身份、权限、到期时间；不回显凭证 |
| `accounts:read` | 新增 `GET /ops/accounts`、`GET /ops/accounts/{id}`：客户、钱包摘要、最近 100 个 Key 元数据（更多时明确 `keys_truncated`） |
| `accounts:write` | 既有账户冻结/解冻；新增 `POST /ops/keys/{id}/status`：Key 冻结/解冻，要求预期状态和原因 |
| `payments:read` | 新增 `GET /ops/orders`、`GET /ops/orders/{id}`：订单和退款状态，不返回收银 URL |
| `payments:write` | 接既有支付核查及全额退款接口，不在前端改订单、钱包或账本；原退款重试使用服务端记录的原幂等键 |
| `payments:risk:write` | 接既有退款风险回收/核销接口，单独授权，不由普通支付写权限推导 |
| `reconciliation:read/write` | 既有待对账队列、新增 `GET /ops/requests/{id}` 及逐 attempt 元数据；已核验未收费才能释放，结算必须提供核验的 Token 与上游成本 |
| `channels:read` | 既有平台渠道状态查询；本批没有网页输入供应商密钥或轮换入口 |
| `models:read/write` | 后续新增模型当前售价/历史、调价与上下架确认流程，详见[模型售价版本验收](MODEL-PRICE-ACCEPTANCE.md) |
| `metrics:read` | 既有 UTC 站点日预算与待对账数量查询 |
| `audit:read` | 新增分页 `GET /ops/audit`，可按 `target_id` 过滤，显示主体、对象、原因、操作 ID、前后状态和时间，不返回凭证或来源 IP 摘要 |

后台读取端点也逐个核验 scope。客户网页登录态、客户 API Key、仅 `console:read` 凭证都不能获得额外后台权限。

## 操作与身份安全

1. 使用既有可信运维端签发的短时分权限 token，默认 5 分钟、最长 15 分钟；新增 UI 用户需 `console:read` 加实际业务 scope。签名密钥不进页面、不通过聊天传输。本批没有签发或修改真实生产凭证。
2. 运维 token 仅在模块闭包内存中；输入后清空输入框。不写浏览器存储、URL、Cookie 或日志；退出/离页清空数据。锁定页面不是服务器撤销已经签发的 token，该 token 仍有效至其到期时间。
3. 生产 `/ops/*` 的受保护入口依旧必须存在。浏览器保留同源 SSO 入口 Cookie，但应用只接受独立的运维鉴权头，不把 Cookie 当作运维权限。token 与入口密钥是两种用途，不得互换。真实 IdP/MFA、身份签发流程和入口部署尚待实网验收，不声称本批完成 MFA。
4. 运维页面使用独立严格 CSP，无第三方脚本、禁止框架嵌入及原生表单提交。所有 API 请求固定同源 `/ops/*`、禁止跳转；文案和元数据使用 textContent，不执行 HTML。
5. 确认页冻结精确路径、对象、观察状态、原因及幂等键。切换语言不改变已准备命令、不发写请求。更换操作或修改原因/核验数字会取消旧确认。
6. 确认只发一次请求，禁止并行重复写；超时/丢响应标记未知，不自动重发。保留原命令供核对，再读原对象及审计。页面退出/身份改变时丢弃迟到响应，不把上一操作者结果显示给下一位。
7. 账户状态支持可选 `expected_status`，运营台始终发送；Key 状态要求该字段。观察状态过期返回 409，需刷新再准备操作，不允许恢复 revoked Key。

## Key 冻结的生命周期

Key 单独冻结保留原 Key、限额与历史账目，禁止新请求；仅活动且验证过邮箱的账户可以恢复 frozen Key。在途已经预授权的调用仍可能完成，不声称撤销发出的模型请求。

密码重置、账户冻结、客户主动吊销现在都覆盖 active 和 frozen Key。被吊销后不可通过解冻恢复。冻结 Key 仍计入 Key 数量上限，不能靠反复冻结制造无限可恢复凭证。账户解冻不恢复旧会话或 Key。

新增 Key 状态操作在 User → ApiKey 行锁内完成，并与 OperatorAction 审计一起提交，与模型受理和密码重置使用一致的锁顺序。

## 验收与复现

本地 Python 422 项通过，分支覆盖率 83%；前端 48 项测试及 TypeScript 类型检查通过。新增 API 测试覆盖 scope、脱敏、分页、冻结/解冻/吊销、冻结 Key 配额与密码重置、过期观察状态、CSP。前端 transport 回归覆盖同源边界、短时过期、401 清理、重复写阻断及退出后的迟到响应。

隔离 PostgreSQL 16 的完整角色/ACL/假 Vault 门禁通过；新增实际 Key 冻结 handler 与模型 admission 的并发测试，冻结提交后新 admission 拒绝，冻结前的占用仍可释放；既有钱包、Key、全站预算与幂等测试保留。没有迁移生产数据库。

浏览器在 `127.0.0.1:8797` 可丢弃夹具验证：只读不能操作；确认前零写；语言切换不改命令；Key 冻结与恢复；账户冻结/恢复后原 Key 仍 revoked；模型释放和按已核验数字结算；假退款执行后丢响应仍只执行一次，查询原订单显示 refunded；审计可见；退出清空数据；移动端不溢出。

```bash
# 仅本地隔离测试，不得作为部署入口
.venv/bin/python tests-browser/ops_fixture.py
# 第二个终端，使用现有 Playwright/Chromium
node tests-browser/ops.cjs

.venv/bin/python -m coverage run --branch -m pytest -q
.venv/bin/python -m coverage report --fail-under=80
npm run check
# 独立、明确标记为 kunlun-ci-disposable 的测试 DB
bash scripts/ci_postgres_gate.sh
```

测试夹具 token/邮件/支付路由均在 `tests-browser/`，不在应用包或生产镜像中。截图在忽略目录 `release-artifacts/ops-review-desktop.png`、`ops-confirm-desktop.png`、`ops-audit-mobile-zh.png`。页面是本地模拟运营台，不是客户案例、真实收费或公网 SLA 证据。

## 仍待完成与回滚

本批不是完整 P0-10：告警聚合与通知闭环、可视化渠道轮换、更多 Key 历史的检索体验、退款风险回收/核销的完整浏览器情景及真实 IdP/MFA/入口验收仍待完成。模型价格上下架/版本操作界面已有后续[独立验收记录](MODEL-PRICE-ACCEPTANCE.md)。客户资料与风险原因只可交付给获授权运营者。

正式支付 SDK/商户、合格模型供应渠道、正式服务政策、预发布与生产、真实小额支付/调用/退款和恢复/容量验收仍按主 PRD 继续。没有声称只补一个账号就能收费上线。

本批无 schema 迁移，仍为 `0015_key_policy`。若需回滚，先锁定运营入口/停止新动作，保留在途结果与账目；不能直接回退到不识别 frozen Key 的旧版本，否则密码重置/冻结账户的撤销语义不完整。优先保留本批冻结凭证覆盖修复并前向修复界面。不要删审计或编辑钱包来回滚。
