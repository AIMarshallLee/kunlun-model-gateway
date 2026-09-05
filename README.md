# Kunlun Model Gateway

Kunlun Model Gateway 当前开发目标是海外商业模型 API 聚合站：客户注册、验证邮箱、购买调用额度、创建本站 Key，通过统一 API 使用平台接入的模型。目标基线见 [商业中转站 PRD](docs/PRD-overseas-commercial-api-gateway.md)。BYOK 保留为独立可选模式，不再替代主产品。项目是 FastAPI + SQLAlchemy 独立部署单元，不进入公众号客户桌面包。

本项目不提供共享订阅账号、来源不明的 Key、无限量承诺、未验证匿名调用、提现或余额转账。`managed_gateway` 模式由平台提供独立 Vault 凭据，客户余额与平台成本预算分别控制；`byok` 模式由客户支付供应商模型费。

当前是商业内核开发候选，尚未完成整站商业化。实现和剩余门槛见 [商业内核验收说明](docs/MANAGED-CORE-ACCEPTANCE.md)。默认关闭公开注册、测试支付和真实上游；支付 bridge 仍只是官方 SDK sidecar 的协议适配层，正式支付渠道尚未选定接通，不能把模拟回调或本地 Docker 称为真实收款。

原 [客户开通与上线验收手册](docs/CUSTOMER-DELIVERY.md) 和生产环境模板仍针对 BYOK，不能直接作为商业站发布指南。下方快速启动是旧额度模式的本地模拟；新商业模式使用显式注入的测试适配器验收，不允许打开旧 `legacy_test` 模式上线。

## 快速启动（本地模拟，非生产 BYOK）

```bash
git clone https://github.com/AIMarshallLee/kunlun-model-gateway.git
cd kunlun-model-gateway
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
export KUNLUN_PUBLIC_SIGNUP=true
export KUNLUN_ENABLE_TEST_PAYMENTS=true
export KUNLUN_PAYMENT_WEBHOOK_SECRET='local-only-secret-change-me'
export KUNLUN_LIVE_UPSTREAM=false
uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload
```

容器构建使用 [requirements-gateway.lock](requirements-gateway.lock) 固定运行依赖；升级依赖必须重新生成锁文件并完整回归。开发环境的 `.[test]` 额外安装测试工具，不应直接当作生产镜像依赖。

本地测试支付只生成订单和验证 HMAC 回调，需由测试脚本构造回调；它不连接任何支付机构、不扣款、不具备提现能力。生产/共享网络环境不要复制这组开关。

运行验证：

```bash
python -m pytest -q
python -m coverage run -m pytest -q && python -m coverage report
```

浏览器访问 `/` 可使用最小开发者控制台，完成注册、登录、Key、额度、预算、用量和账本查看；会话只保存在页面内存，刷新即退出，主动退出会服务端吊销该账户的全部网页登录会话。健康检查为 `GET /healthz`，就绪信息为 `GET /readyz`。开发环境 API 文档在 `/docs`；生产配置同时关闭 `/docs` 与 `/openapi.json`。

## API 最小流程

1. `POST /auth/register` 注册（仅 `KUNLUN_PUBLIC_SIGNUP=true` 时可用）。
2. `POST /auth/login` 获取用户会话 Bearer Token。
3. `POST /v1/keys` 创建 API Key。原始 Key 只返回一次，数据库只保存摘要；丢失后只能吊销并新建。
4. 受控验证阶段：开发环境可用 `POST /billing/topups` 创建测试订单并向 `POST /billing/webhook` 提交 HMAC 测试回调；正式环境的 `POST /billing/checkout` 仅通过独立 HTTPS payment bridge 调用官方 SDK sidecar，`POST /billing/live/webhook` 接收验签回调。没有真实商户凭据时保持关闭。
5. `GET /billing/balance` 查看服务额度，`GET /billing/ledger` 查看只追加账本，`GET /billing/costs` 查看请求成本。
6. `POST /v1/chat/completions` 使用 API Key 调用；所有 `managed_gateway` 和生产 BYOK 请求必须携带稳定的 `Idempotency-Key`，`GET /v1/models` 查看已定价模型。

请求调用前会按模型价格和输出上限预授权预算；上游返回完整有效 usage 时按实际 Token 结算并释放差额。BYOK 响应缺少或返回非法 usage 时不做估算结算，而是保留预授权并进入 `pending_reconciliation`；状态不确定的超时/异常同样不自动切换或静默退款。

Key 创建支持可选模型范围、单次输出上限和累计消费上限；控制台支持中英文设置和查询。累计消费不自动重置，待对账请求继续占用，预算检查在审核/模型外呼之前执行。接口、迁移与并发验收详见 [Key 权限与累计上限](docs/KEY-POLICY-ACCEPTANCE.md)。

独立运营台入口 `/ops/console`，使用短时分权限运维凭证，不接受客户 Key。可查询客户/订单/待对账/渠道/预算/审计，确认后执行已有运维流程。能力、权限、模拟验收及未完成项见 [运营台交付记录](docs/OPS-CONSOLE-ACCEPTANCE.md)。

商业模式已有模型售价版本、上下架与历史查询；调价不重算旧请求，重启不覆盖运营目录。接口、权限、并发锁及回滚限制见 [模型售价验收](docs/MODEL-PRICE-ACCEPTANCE.md)。

运营台可聚合预算、供给、售价、模型与支付异常，并记录“已知悉”；确认不会解除告警或释放资金。覆盖范围、权限及尚未接通的外部通知见 [运营告警验收](docs/OPS-ALERTS-ACCEPTANCE.md)。

新增默认只预览的[告警邮件摘要 worker](docs/ALERT-NOTIFICATION-ACCEPTANCE.md)，复用持久化 outbox 与 SMTP，限制重复发送并保留未知结果。真实收件人/调度尚未启用，SMTP accepted 不等于收件箱送达。

商业财务链路新增[独立拒付记录、冲正与风险处置 API](docs/CHARGEBACK-ACCEPTANCE.md)，schema head 为 `0016_chargebacks`。部分/重叠事件保留待对账；正式支付 SDK、返还处理与拒付专用 UI 仍未完成。

## OpenCode / OpenAI-compatible 接入

自有工具可从[可运行 Python 接入样例](docs/OWN-TOOL-INTEGRATION.md)开始：默认预览，显式执行才调用模型；覆盖非流式、SSE、工具字段透传和原任务查询。仅为隔离模拟验收样例，不代表真实客户接入。

把网关作为 OpenAI-compatible provider，核心配置是：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "kunlun-gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Kunlun Gateway",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "{env:KUNLUN_GATEWAY_API_KEY}"
      },
      "models": {
        "test-model": {
          "name": "Kunlun test-model",
          "limit": {"context": 128000, "output": 4096}
        }
      }
    }
  },
  "model": "kunlun-gateway/test-model"
}
```

该示例对应已验证的 OpenCode `1.18.21` V1 配置格式；升级 OpenCode 时必须重新按官方 schema 和插件事件契约验证。`baseURL` 必须以 `/v1` 结尾，API Key 应由 `{env:...}` 或 OpenCode `/connect` 注入，不要提交到仓库。可参考 [opencode.example.json](opencode.example.json)。网关支持 OpenCode 所需的 SSE `stream=true` 和工具字段透传：流开始前完成预授权，收到完整 `[DONE]` 且完整 usage 后结算；断流或用量不确定时保留预授权并进入人工对账，首字节发出后绝不切换供应商。

不要直接覆盖现有公众号工作区配置。先执行只读预览，确认保留原有 MCP 与权限字段后再显式应用：

```bash
cd gateway-platform
python3 -m scripts.opencode_install --target ../opencode.json
python3 -m scripts.opencode_install --target ../opencode.json --apply
export KUNLUN_GATEWAY_API_KEY='仅粘贴创建时显示一次的 gw_... Key'
```

如果通过 Wheel 安装，也可使用等价命令 `kunlun-opencode-install --target /你的工作区/opencode.json`。默认只预览；写入时必须同时显式提供 `--target` 和 `--apply`，脚本不会根据安装目录猜测工作区。

应用时还会原子安装 `.opencode/plugins/kunlun-gateway-idempotency.js`：OpenCode 1.18.21 只会自动发现该目录中的 `.js`/`.ts` 文件；插件只导出一个入口，并只对 `kunlun-gateway` provider 添加基于 session/当前 assistant turn 的稳定 `Idempotency-Key`。同一 OpenCode 重试复用同一键，新 turn 生成新键。目标插件已存在且内容不同时安装器会拒绝覆盖。默认不会把网关设为 OpenCode 的默认模型；只有明确需要时再增加 `--set-default-model`。写入前会生成 `.pre-kunlun-gateway.bak` 可恢复备份，脚本不接收、读取或持久化 API Key。

## 环境变量

复制 `.env.example` 后按部署环境填值。所有金额使用整数 `microUSD`：`1 USD = 1,000,000 microUSD`，接口字段 `amount` 也采用该单位，禁止浮点金额。

| 变量 | 默认/示例 | 说明 |
| --- | --- | --- |
| `KUNLUN_ENV` | `development` | 生产必须是 `production`，触发失败关闭门禁 |
| `KUNLUN_DATABASE_URL` | SQLite 文件 | 生产必须是 PostgreSQL |
| `KUNLUN_VAULT_EXECUTOR_DATABASE_URL` | 空 | 生产 BYOK 使用独立 `kunlun_vault_executor` 的 verify-full URL；不得与 runtime 复用数据库密码 |
| `KUNLUN_PUBLIC_SIGNUP` | `false` | 仅供开发/受控测试；邮件验证与注册反滥用完成前，生产会拒绝开启 |
| `KUNLUN_ENABLE_TEST_PAYMENTS` | `false` | 仅本地 HMAC 假支付；生产禁止 |
| `KUNLUN_LIVE_PAYMENTS` | `false` | 仅在 payment bridge、官方 SDK/证书、商户资质与小额联调完成后开启 |
| `KUNLUN_LIVE_UPSTREAM` | `false` | legacy_test 兼容路径；BYOK 不会从环境变量读取客户 Provider Key |
| `KUNLUN_GATEWAY_MODE` | `legacy_test` | `disabled`、`byok` 或仅本地兼容的 `legacy_test`；生产 BYOK 必须使用 Supabase Vault |
| `KUNLUN_VAULT_BACKEND` | `disabled` | 生产 BYOK 只能是 `supabase_vault`；启动时会执行受控 probe，缺少迁移、Vault 扩展或 runtime 权限即失败关闭 |
| `KUNLUN_PAYMENT_WEBHOOK_SECRET` | 空 | 测试回调密钥；不得写入响应或日志 |
| `KUNLUN_API_KEY_PEPPER` | 随机生成 | 多实例/生产必须固定并通过 Secret Manager 注入 |
| `KUNLUN_SESSION_PEPPER` | 随机生成 | 多实例/生产必须固定并通过 Secret Manager 注入 |
| `KUNLUN_PUBLIC_BASE_URL` | 空 | 公开站点的 HTTPS 根地址；支付 `return_url` 必须与它同源 |
| `KUNLUN_TRUSTED_PROXY_CIDRS` | 空 | 生产必须显式配置可信反向代理；内置 compose 只信任固定 Caddy `172.30.50.2/32`，其余来源的转发头被忽略 |
| `KUNLUN_TRUSTED_PROXY_SECRET` | 空 | 入口适配层 → 应用的共享代理密钥；至少 32 个可打印 ASCII 字符，只能通过部署平台 Secret 注入，应用验证后会删除内部请求头 |
| `KUNLUN_INGRESS_PROVIDER` | 空 | Vercel 容器部署显式设为 `vercel`；适配层只使用 Vercel 覆盖后的客户端 IP 头，并删除调用方提交的转发头 |
| `CRON_SECRET` | 空 | Vercel Cron 的独立 32+ 字符 Bearer 密钥；不得与代理密钥或 Pepper 复用 |
| `KUNLUN_OPS_INGRESS_SECRET` | 空 | Vercel 运维入口的独立 32+ 字符门禁；正确值只允许请求进入应用层，应用层仍要求有 scope 的短期运维 Token |
| `KUNLUN_OPERATOR_SIGNING_SECRET` | 空 | 生产运维短令牌签名密钥；通过 `scripts/mint_ops_token.py` 颁发最小 scope、短 TTL Token |
| `KUNLUN_PROVIDERS_JSON` | `[]` | BYOK 供应商名称、固定 `base_url`、models 和价格目录；禁止 `api_key_env` 和任何共享密钥 |
| `KUNLUN_PROVIDER_HOST_ALLOWLIST` | 空 | 开启真实上游前必须列出精确 Provider hostname；生产还需固定出口网络策略 |
| `KUNLUN_MODELS_JSON` | 开发内置 `test-model` | 模型售价及上限；生产开启真实上游时必须显式配置，不能沿用测试默认值 |
| `KUNLUN_RATE_LIMIT_PER_MINUTE` | `60` | 单 Key 的数据库窗口限流；生产仍需边缘限流/熔断 |
| `KUNLUN_CHECKOUT_RATE_LIMIT_PER_MINUTE` | `5` | 单用户/IP 每分钟 checkout 上限；先限流再触发数据库或支付 bridge |
| `KUNLUN_MAX_OPEN_CHECKOUT_ORDERS` | `3` | 单用户未关闭订单上限；支付成功、失败或关闭后释放名额 |
| `KUNLUN_MAX_OUTPUT_TOKENS` | `4096` | 平台输出上限 |
| `KUNLUN_DEFAULT_OUTPUT_TOKENS` | `256` | 未指定时的预授权输出上限 |
| `KUNLUN_TERMS_URL` | 空 | 公开注册生产门禁 |
| `KUNLUN_PRIVACY_URL` | 空 | 公开注册生产门禁 |
| `KUNLUN_COMPLAINT_EMAIL` | 空 | 公开注册生产门禁 |
| `KUNLUN_COMPLIANCE_ACKNOWLEDGED` | `false` | 完成法律/合规评估后显式确认 |

BYOK 运行边界：`development` 与 `staging` 均禁止 BYOK 外呼；`test` 仅在 `create_app(..., credential_vault=...)` 显式注入测试 Vault 时允许，用于自动化测试，不读取环境变量中的客户密钥。生产必须使用 `KUNLUN_GATEWAY_MODE=byok` 与 `KUNLUN_VAULT_BACKEND=supabase_vault`。

生产 BYOK 供应商目录示例（仅服务端允许列表与预授权价格，不含任何 Key）：

```json
[
  {"name":"openai","base_url":"https://api.openai.com/v1","models":["example-model"],"pricing":{"example-model":{"input_microusd_per_million":1200000,"output_microusd_per_million":3600000}}},
  {"name":"deepseek","base_url":"https://api.deepseek.com/v1","models":["example-model"],"pricing":{"example-model":{"input_microusd_per_million":1300000,"output_microusd_per_million":3900000}}}
]
```

上述模型 ID 和价格只是配置形状示例，不是当前官方价目或已联调声明。上线前必须根据官方文档重新核对 endpoint、模型 ID、计费单位与条款；客户 Key 只能通过 Provider Connection 接口进入每客户独立 Vault 记录。
首发生产目录只允许 OpenAI、DeepSeek 和 Google Gemini 的官方 OpenAI-compatible endpoint，供应商名与 scheme/hostname/path 硬绑定。Anthropic 虽提供 OpenAI SDK 兼容层，但官方明确说明其在多数场景不是长期生产方案，因此在完成原生适配器和真实联调前不进入首发允许目录。

## 预算、成本与故障切换边界

- 预授权、上游调用、结算是三个独立阶段；不会把上游网络调用放进数据库事务。
- 钱包和预算使用整数余额；账本和带明确 `target_type/target_id` 的运维审计只追加。SQLite 由防御性触发器禁止 UPDATE/DELETE，PostgreSQL 迁移同时禁止 UPDATE/DELETE/TRUNCATE，runtime 角色只有 SELECT/INSERT；生产预检会验证实际权限和守卫。
- 替换本月预算会继承本月已发生支出；新上限不能低于已发生支出。有未结算预授权时拒绝替换，预算替换与新预授权通过客户钱包行串行化，避免旧预算继续承接新请求。
- 允许切换：上游尚未接收请求的连接失败，以及明确拒绝且不计费的 429。其他错误只有在供应商书面计费契约和适配器测试证明“不可能计费”后才能显式放行。
- 禁止自动切换：500/502/503、读/写超时、参数/认证/权限等 4xx、内容审核拒绝、响应解析失败、客户端断开、已经向客户端发出流式首字节及其他不确定状态。5xx 默认按“可能已受理并计费”进入人工对账，避免两个供应商重复执行和双份成本。
- 每次请求记录 request ID、供应商尝试顺序、模型、Token、估算标记、预授权/实际成本、失败类别和最终供应商；不记录提示词、回答、请求正文或正文哈希。
- 开启内容安全时，输入检查对象与真正发给供应商的规范化 payload 一致，覆盖 `messages`、`tools`、`tool_choice`、`response_format` 等字段；任何检查失败都发生在预授权和上游调用之前。
- `/ops/reconciliation`、退款、风险处置与指标接口均需要边缘入口密钥和应用内有最小 scope 的运维短令牌两层门禁。生产不使用旧的静态 `KUNLUN_OPERATOR_TOKEN` 兼容路径。人工释放必须确认上游未计费；人工结算必须提交已核对的输入/输出 Token 与上游成本，并生成带目标对象的不可变运维动作记录。
- checkout 先以数据库 CAS 取得唯一五分钟调用租约，并把客户 `Idempotency-Key` 透传给 sidecar；并发请求只有一个能调用外部支付。调用超时或租约过期不盲目重试，只能按商户订单号人工查询/对账。退款命令同样先持久化后调用支付桥，进程崩溃可由同一幂等键租约重领或认证 webhook 完成。
- 同一用户的支付入账和退款最终化都锁定钱包行。现金已退但可用额度不足时会扣尽剩余额度、记录差额风险并冻结账户；在途预授权全部结算/释放后，持有 `payments:risk:write` 的财务操作员才可选择全额追回或显式计入 `PLATFORM_LOSS`。处置不会自动解冻，存在其他风险或邮箱未验证时仍禁止解冻。
- checkout 在触发支付 bridge 前执行用户/IP 限流和单用户未关闭订单上限；过期调用租约只进入查询/对账，不会静默新建第二个支付意图。`payment.closed` 会终结未支付订单并释放名额。
- 数据库限流窗口由独立 maintenance 进程每 60 秒删除七天前的计数，同时恢复超时模型预占到 `pending_reconciliation`；业务 API 不承担清理循环，这不替代公网边缘限流。
- 当前限流是数据库固定窗口实现。多副本生产仍应在 Redis/WAF/API Gateway 增加边缘限流与熔断，并保留 PostgreSQL 资金事务和监控。

## 已实现、待验证与不做事项

已实现（代码与本地验证范围）：注册/登录、邮件验证/密码恢复服务、API Key 摘要保存与吊销、Turnstile hostname/action 服务端绑定、可信代理 IP 边界、测试 HMAC 订单回调幂等、现金支付与服务额度分离的整数双式账本、余额/预算预授权与结算、OpenAI-compatible 非流式与 SSE 流式接口、工具调用字段透传及完整输入安全检查、流式断线/5xx 待对账、供应商受限故障切换、BYOK 每客户 Vault 连接、预算硬上限和 attempt/最终请求分账、成本台账、OpenCode 幂等插件、输入输出内容安全流式响应上限、运维短令牌、checkout 外部副作用幂等租约与防滥用上限、支付/退款可发现对账队列与持久化认领租约、可恢复退款及财务风险处置、审计目标与数据库不可变守卫、限流数据保留任务、Alembic `0001→0013` 迁移链及敏感正文不落日志的测试覆盖。本地自动化通过不等于真实 Supabase、持续压测、供应商计费或客户验收证据。

待验证（外部证据尚未提供）：真实官方供应商联调、各供应商 5xx/usage/计费契约、PostgreSQL 持续并发压测、Redis/边缘限流、正式支付 SDK/证书/商户与小额支付退款对账、SMTP 送达与域名 SPF/DKIM/DMARC、真实域名绑定的 Turnstile site key/secret 与浏览器到服务端二次校验、内容安全和投诉流程、TLS/WAF/KMS、**生产 Supabase Vault/KMS 适配与密钥销毁演练**、备份恢复演练、真实客户验收以及公网灰度。浏览器 Turnstile 组件及服务端 Siteverify 适配已经实现，但没有真实密钥与域名验收就不算上线证据。

不做：共享账号、低价 Key 倒卖、匿名公共 API 充值、明文密钥入库、无限量承诺、静默超支、静默跨供应商切换、自动发布公众号内容、把测试支付包装成真实支付、在没有合规与商户资质证据时宣布公网/生产可用。

## 真实上线门禁

`KUNLUN_ENV=production` 会拒绝 SQLite、测试支付、弱 Pepper，并对公开注册、真实上游、真实支付执行独立配置门禁。代码门禁通过也只是“候选生产基线”，不代表已经有域名、邮件、商户或真实支付证据。真正公网发布还需要在部署前完成：

1. PostgreSQL、Redis/边缘限流、TLS/WAF、密钥托管、备份恢复和告警值班。
2. 正式支付服务商的官方 SDK sidecar、签名/证书校验、退款/争议/对账和幂等处理；网关只提供 provider-neutral bridge，不代替商户接入或支付机构资质。
3. 服务条款、隐私政策、数据留存/删除、滥用处置、人工申诉和内容安全策略。
4. 对所在地互联网信息服务、增值电信、生成式 AI 服务、算法/模型备案及应用展示信息的专业核验；文末提供官方参考入口，不能替代法律意见。
5. 至少一次真实上游联调、恢复演练、账本对账和小流量灰度；“测试全绿”不等于“公网可用”。

## Docker 开发环境

```bash
cd gateway-platform
docker compose -f docker-compose.dev.yml up --build
```

该 compose 只启动 API、一次性 Alembic migrator、PostgreSQL 与 Caddy；API 使用 `kunlun_runtime` 非 owner 角色，迁移使用独立 `kunlun_migrator` 角色，只有 Caddy 暴露 80/443。默认测试支付、公开注册和真实上游均保持关闭。需要本地注册/测试充值时，在 API 容器外显式设置对应环境变量并承担其测试性质；不要把 `.env.production` 提交到仓库。

## Docker Compose 生产密钥范围

生产 Compose 只连接已经完成三连接预检的外部 Supabase PostgreSQL；它不启动本地
PostgreSQL、不创建数据库角色，也不挂载 `init-postgres-roles.sh`。复制
`.env.production.example` 到受控的 `.env.production` 后，以显式 env 文件启动：

```bash
docker compose --env-file .env.production -f docker-compose.production.yml up --build
```

该 env 文件只提供 Compose 插值，**不会**作为任意服务的全量 `env_file` 注入。长期 API 容器仅取得 runtime 与 Vault executor URL 及业务运行必需变量；长期 maintenance 容器仅取得 runtime URL 与其清理配置，不持有 Vault 或迁移凭据。一次性 `migrate` 容器只有 migrator URL；一次性 `preflight` 可在启动检查期间取得三条数据库 URL，完成即退出。数据库管理员建角和 Supabase Vault bootstrap 必须在 Compose 之外、由受控管理员流程完成；不要把管理员凭据或 migrator URL 配置到 API、maintenance、Vercel 或 Cloudflare 的长期运行环境。Compose 使用 Caddy 固定地址 `172.30.50.2/32`，因此示例中的 `KUNLUN_INGRESS_PROVIDER` 保持为空。

## GitHub → Cloudflare Containers

本仓库包含 `wrangler.jsonc` 和边缘 Worker，可由 Cloudflare Workers Builds 从 GitHub 的 `main` 分支构建 Dockerfile 并发布。该路径依赖 Cloudflare Workers Paid、外部托管 PostgreSQL、Cloudflare 账户授权和运行时 Secrets；Cloudflare Pages 不能替代这个 FastAPI + PostgreSQL 后端。

边缘 Worker 固定执行以下安全策略：

- 公网 `/ops`、`/ops/*` 和 `/metrics` 始终返回 404，包括常见 URL 编码绕过；
- 覆盖客户提交的代理头，并使用 `KUNLUN_TRUSTED_PROXY_SECRET` 认证 Worker → Container 的客户端 IP；
- 公开注册、测试支付、真实支付和真实模型上游固定关闭；当前 Cloudflare 配置只用于受控 staging，不是匿名公共 API 充值站；
- 一个固定 Container 实例承载 API，另一个固定实例由 Cron Trigger 每五分钟执行一次 maintenance；不会把容器临时磁盘当数据库；
- 缺少 PostgreSQL URL 或任一核心 Secret 时，Worker 返回不含敏感值的 503，不启动业务 Container。

首次连接前，先在独立受控环境完成数据库步骤：

1. 创建专用 Supabase PostgreSQL project，并建立不同的 `kunlun_migrator`、`kunlun_runtime` 与 `kunlun_vault_executor` 角色；三条数据库 URL 的密码必须两两独立（百分号编码后仍按解码值比较），连接必须使用 TLS。生产预检同时比较 Supabase project ref、database name 与迁移创建的随机安装 UUID，不会输出这些标记。数据库 clone/restore 必须作为新 project 重跑三连接预检，禁止把源库与 clone 混用。
2. 若启用 Supabase Vault，先由项目特权管理员运行 `scripts/bootstrap_supabase_vault.sql`。
3. 用 `kunlun_migrator` URL 执行 `python -m alembic upgrade head`。
4. 用 runtime URL 与独立 migrator URL 执行 `python -m scripts.preflight`，确认 schema 精确位于当前 head 且 runtime 无迁移、历史账本修改或审计篡改权限。
5. 只有预检通过后，才向 Cloudflare 配置下列 Worker Secrets：

```text
KUNLUN_DATABASE_URL=postgresql+psycopg://<runtime role>@<external host>/<database>?sslmode=verify-full&sslrootcert=/app/certs/supabase-prod-ca-2021.crt
KUNLUN_GATEWAY_MODE=byok
KUNLUN_VAULT_BACKEND=supabase_vault
KUNLUN_API_KEY_PEPPER=<32+ random printable ASCII characters>
KUNLUN_SESSION_PEPPER=<different 32+ random printable ASCII characters>
KUNLUN_TRUSTED_PROXY_SECRET=<different 32+ random printable ASCII characters>
```

不要把 migrator URL 注入长期运行的 Worker/Container，也不要把任何真实值写进 `wrangler.jsonc`、GitHub Actions 或仓库文件。当前 Cloudflare 配置没有完成 BYOK Vault 环境传递与真实数据库验收，只能视为保留的 staging 路径，不是本轮生产发布方案。
`0008` 之后的安全迁移要求数据库实际登录角色名精确为 `kunlun_migrator`；其 downgrade 保持权限收紧，不会重新授予 Supabase Data API 角色。生产降级只能在维护窗口、显式破坏性确认和已验证备份下执行。

### Supabase Vault（BYOK）

`0012_supabase_vault` 把密钥正文保留在 Supabase Vault：应用 runtime 角色没有
`vault.secrets`、`vault.decrypted_secrets` 或私有绑定表的直接权限，也不能调用密钥解析函数。
独立的 `kunlun_vault_executor` 连接只能调用私有、固定 `search_path` 的受控函数。
解析和吊销会同时核验用户、连接、供应商、版本和
opaque `vault_ref`；接口、业务表、审计记录和日志都不得保存或返回明文。

这仍是候选实现，不是已完成的生产 Vault 验收。首次生产启用必须严格按以下顺序：

1. 由 Supabase 项目特权管理员先运行 `scripts/bootstrap_supabase_vault.sql`，建立
   `kunlun_private` 及 Vault owner 权限；不要用 runtime 角色替代管理员。
2. 使用独立的 `kunlun_migrator` URL 执行 `python -m alembic upgrade head`，使
   `0012_supabase_vault` 创建并锁定私有函数契约。
3. 最后同时提供 runtime、独立 migrator 和独立 Vault executor 三条 URL，执行 `python -m scripts.preflight`，并
   检查 `/readyz` 的 Vault probe、同租户解析、跨租户/旧版本拒绝、轮换和吊销后的
   不可解析；任何一项失败都保持 BYOK 不可用。

连接吊销采用两阶段流程：若 Vault 物理删除失败，接口返回 HTTP `202` 和
`revoked_pending_destroy`；对同一连接重复执行清理是幂等的，待 Vault 恢复后可重试，
直到状态不再是 `revoked_pending_destroy`。该状态不能被当作“密钥已完成销毁”。

Supabase 部署镜像内置其公开的 `Supabase Root 2021 CA`，运行时使用 `verify-full` 同时验证 CA 与数据库主机名；证书 SHA-256 指纹为 `80:70:25:AD:50:D4:ED:21:9D:2C:9C:7D:29:9C:00:4F:82:4E:B0:0C:F7:F6:5A:FE:F6:07:D0:7B:72:E6:CA:FA`，有效期至 2031-04-26。替换证书前必须从 Supabase 官方控制台重新下载并复核指纹。

在 Cloudflare 控制台安装 **Cloudflare Workers and Pages** GitHub App 时，只授权本仓库；选择 `main` 为生产分支，构建命令使用 `npm ci && npm run check`，部署命令使用 `npm run deploy`，根目录为仓库根目录。Cloudflare Workers Builds 的生产部署会构建 Dockerfile；非生产分支的 `versions upload` 不会发布 Container 镜像，不能当作完整预览环境。

本地可在已登录 Cloudflare 且 Docker 可用时执行：

```bash
npm ci
npm run check
npx wrangler deploy
```

如果本机没有 Docker，只能用 `npx wrangler deploy --dry-run --containers-rollout=none` 检查 Worker bundle；这不构建 Container，也不是部署证据。首次部署后还必须实测 `/healthz`、`/readyz`、受保护接口、编码后的私有路径、Container 休眠重启、Cron maintenance、账本持久性和数据库恢复。Workers Builds、Secrets、域名、WAF 与 PostgreSQL 均属账户侧配置，单纯推送 GitHub 不代表 Cloudflare 已上线。

## GitHub → Vercel Container

Vercel 路径使用仓库根目录的 `Dockerfile.vercel`，保留原 FastAPI、非 root 用户和 Supabase CA，并通过平台提供的 `$PORT` 启动。`vercel.json` 把函数固定在 Montréal `yul1`，并每五分钟调用一次受 `CRON_SECRET` 保护的 `/api/cron/maintenance`。该频率和商业用途要求 Vercel Pro 或更高计划；Hobby 不能作为本产品的商业上线环境。

Vercel Functions 当前不能直接访问 Supabase 默认的 IPv6 数据库端点，因此长期 Runtime 连接应使用项目 Connect 面板提供的 IPv4 Supavisor **session pooler** URL。URL 用户名形如 `kunlun_runtime.PROJECT_REF`，仍须使用 `postgresql+psycopg`、`sslmode=verify-full` 和镜像内绝对 CA 路径。数据库迁移继续通过外部一次性 `kunlun_migrator` 直连执行；严禁把 migrator URL 放进 Vercel。

Vercel 项目最小生产变量：

可复制 `.env.vercel.production.example` 作为 Vercel 完整运行变量录入清单，
再用 Secret Manager 填入独立密钥、通过预检的 runtime/Vault executor URL 和已核验的
Provider/模型目录。该模板不包含 migrator 或管理员凭据；建角、Vault bootstrap、
迁移和三连接预检仍必须在 Vercel 之外完成。Vercel ingress 的四项设置不得与
Compose 的 Caddy `/32` 配置混用。

```text
KUNLUN_ENV=production
KUNLUN_DATABASE_URL=<Supavisor session runtime URL with verify-full CA>
KUNLUN_VAULT_EXECUTOR_DATABASE_URL=<separate Supavisor session executor URL with verify-full CA>
KUNLUN_PUBLIC_SIGNUP=false
KUNLUN_ENABLE_TEST_PAYMENTS=false
KUNLUN_LIVE_PAYMENTS=false
KUNLUN_LIVE_UPSTREAM=false
KUNLUN_GATEWAY_MODE=byok
KUNLUN_VAULT_BACKEND=supabase_vault
KUNLUN_PROVIDERS_JSON=<provider/model catalog JSON; no key material>
KUNLUN_PROVIDER_HOST_ALLOWLIST=<exact provider hostnames, comma-separated>
KUNLUN_MODELS_JSON=<explicit model catalog and pre-authorisation ceilings>
KUNLUN_OPERATOR_SIGNING_SECRET=<32+ random printable ASCII characters>
KUNLUN_OPS_INGRESS_SECRET=<different 32+ random printable ASCII characters>
KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED=true
KUNLUN_COMPLIANCE_ACKNOWLEDGED=true
KUNLUN_API_KEY_PEPPER=<32+ random printable ASCII characters>
KUNLUN_SESSION_PEPPER=<different 32+ random printable ASCII characters>
KUNLUN_IDENTITY_TOKEN_PEPPER=<different persistent 32+ random printable ASCII characters>
KUNLUN_TRUSTED_PROXY_SECRET=<different 32+ random printable ASCII characters>
KUNLUN_INGRESS_PROVIDER=vercel
CRON_SECRET=<different 32+ random printable ASCII characters>
```

`KUNLUN_PROVIDERS_JSON` 只放 allowlist、固定 HTTPS 地址、模型和上游价格，不放客户
Key 或共享密钥；BYOK 客户密钥通过每客户独立的 Vault 连接保存。`KUNLUN_MODELS_JSON`
中的每个模型价格是预授权成本上界，不是随意展示价；对每个 provider 的同名模型，
其 input/output 价格都必须小于或等于该上界，否则生产配置会被拒绝。`KUNLUN_OPERATOR_SIGNING_SECRET`、
`KUNLUN_OPS_INGRESS_SECRET` 与 `KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED` 是真实上游/运维接口的门禁，值必须通过
Vercel Secret 注入，不能写入仓库。

Vercel ingress 对 `/metrics` 永久返回 404；对 `/ops`、`/ops/*` 及其常见编码变体，缺少、错误或重复 ingress secret 时同形 404，只有单一正确 `KUNLUN_OPS_INGRESS_SECRET` 才会在删除该请求头后透传给应用。应用层仍必须验证有最小 scope 的短期运维 Token。这是公网控制面的双门禁，不等于私有网络；生产还必须叠加并验收 WAF、固定出口/IP allowlist 或身份代理。生产部署后必须实测数据库连通性、schema head、运维双门禁、Cron 401/200、冷启动、流式响应与重部署后的账本持久性；部署成功本身不等于真实模型、供应商计费或客户验收。

## 部署、迁移与回滚

生产使用 Alembic 迁移链：启用 Supabase Vault 时，先由特权管理员运行 `scripts/bootstrap_supabase_vault.sql`，再由 `kunlun_migrator` 执行 `python -m alembic upgrade head`；API 只用 `kunlun_runtime` 运行且不调用 `create_all`。发布前必须先备份并完成恢复演练；禁止直接在生产删除账本或钱包记录。迁移 head 必须与代码要求精确一致。

隔离 PostgreSQL 验证库可执行 `KUNLUN_CONFIRM_TEST_DATABASE=YES_ISOLATED_TEST_DATABASE KUNLUN_TEST_POSTGRES_URL=... python -m scripts.verify_postgres_concurrency`，复核当前 head、同用户不同订单并发退款、checkout claim 排他性、钱包/账本一致和冻结风险路径。该脚本会写入验证用户、订单、退款和不可改写账本行；未设置显式确认值时会在任何写入前拒绝执行，严禁指向生产库。

推荐发布顺序：备份数据库与配置 → 部署候选镜像 → 运行 `/healthz`、`/readyz` 和只读账本检查 → 小流量灰度 → 观察上游失败率、pending reconciliation、余额对账差异 → 扩大流量。回滚时切回上一镜像并保留数据库，不回滚/改写已入账交易；未完成请求进入人工对账队列。任何数据库 schema 变更必须先做可恢复备份并提供向前/向后兼容方案。

## 合规参考（上线前请由专业人士复核）

- 国家网信办《生成式人工智能服务管理暂行办法》：<https://www.cac.gov.cn/2023-07/13/c_1690898327029107.htm>
- 国家网信办关于开展生成式人工智能服务备案工作的公告：<https://www.cac.gov.cn/2024-04/02/c_1713729983803145.htm>
- 国务院关于非银行支付机构监督管理条例：<https://app.www.gov.cn/govdata/gov/202312/17/510339/article.html>
- 工业和信息化部互联网信息服务管理相关规定入口：<https://sdca.miit.gov.cn/zwgk/fgbz/art/2026/art_fea940f81f1d423e87101adf147ab979.html>
