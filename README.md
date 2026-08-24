# Kunlun Model Gateway

Kunlun Model Gateway 是一个 FastAPI + SQLAlchemy 的模型网关控制面：提供账户、API Key、服务额度、整数 microUSD 成本台账、预算预授权/结算，以及受限的 OpenAI-compatible 多供应商调用。它是独立部署单元，不进入公众号客户桌面包。正式充值把“客户支付现金”（`payment_amount_minor` + `payment_currency`）与“可消费服务额度”（`credit_amount_microusd`）分开记账，禁止把二者混成一个金额字段。

当前交付是“可审计的单节点候选生产基线”，不是已经完成商户收款、备案或公网运营的产品。默认关闭公开注册、测试支付和真实上游；支付 bridge 只是与官方 SDK sidecar 的协议适配层，不能把测试回调、服务额度或本地 Docker 环境称为真实收款。

## 快速启动（本地、默认安全开关）

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
6. `POST /v1/chat/completions` 使用 API Key 调用；`GET /v1/models` 查看已定价模型。

请求调用前会按模型价格和输出上限预授权余额/预算；上游返回真实 usage 时按实际 Token 结算并释放差额，缺少 usage 时标记 `usage_estimated=true`。调用失败会释放预授权，状态不确定的超时/异常会进入 `pending_reconciliation`，不自动切换或静默退款。

## OpenCode / OpenAI-compatible 接入

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
          "limit": {"output": 4096}
        }
      }
    }
  },
  "model": "kunlun-gateway/test-model"
}
```

该示例对应当前项目内 OpenCode Desktop 使用的 V1 配置格式；OpenCode V2 的字段已发生变化，升级底座时必须重新按官方 schema 验证。`baseURL` 必须以 `/v1` 结尾，API Key 应由 `{env:...}` 或 OpenCode `/connect` 注入，不要提交到仓库。可参考 [opencode.example.json](opencode.example.json)。网关支持 OpenCode 所需的 SSE `stream=true` 和工具字段透传：流开始前完成预授权，收到完整 `[DONE]` 后结算；断流或用量不确定时保留预授权并进入人工对账，首字节发出后绝不切换供应商。

不要直接覆盖现有公众号工作区配置。先执行只读预览，确认保留原有 MCP 与权限字段后再显式应用：

```bash
cd gateway-platform
python3 -m scripts.opencode_install --target ../opencode.json
python3 -m scripts.opencode_install --target ../opencode.json --apply
export KUNLUN_GATEWAY_API_KEY='仅粘贴创建时显示一次的 gw_... Key'
```

如果通过 Wheel 安装，也可使用等价命令 `kunlun-opencode-install --target /你的工作区/opencode.json`。默认只预览；写入时必须同时显式提供 `--target` 和 `--apply`，脚本不会根据安装目录猜测工作区。

默认不会把网关设为 OpenCode 的默认模型；只有明确需要时再增加 `--set-default-model`。写入前会生成 `.pre-kunlun-gateway.bak` 可恢复备份，脚本不接收、读取或持久化 API Key。

## 环境变量

复制 `.env.example` 后按部署环境填值。所有金额使用整数 `microUSD`：`1 USD = 1,000,000 microUSD`，接口字段 `amount` 也采用该单位，禁止浮点金额。

| 变量 | 默认/示例 | 说明 |
| --- | --- | --- |
| `KUNLUN_ENV` | `development` | 生产必须是 `production`，触发失败关闭门禁 |
| `KUNLUN_DATABASE_URL` | SQLite 文件 | 生产必须是 PostgreSQL |
| `KUNLUN_PUBLIC_SIGNUP` | `false` | 仅供开发/受控测试；邮件验证与注册反滥用完成前，生产会拒绝开启 |
| `KUNLUN_ENABLE_TEST_PAYMENTS` | `false` | 仅本地 HMAC 假支付；生产禁止 |
| `KUNLUN_LIVE_PAYMENTS` | `false` | 仅在 payment bridge、官方 SDK/证书、商户资质与小额联调完成后开启 |
| `KUNLUN_LIVE_UPSTREAM` | `false` | 开启后读取供应商 JSON 并从环境变量取上游密钥 |
| `KUNLUN_PAYMENT_WEBHOOK_SECRET` | 空 | 测试回调密钥；不得写入响应或日志 |
| `KUNLUN_API_KEY_PEPPER` | 随机生成 | 多实例/生产必须固定并通过 Secret Manager 注入 |
| `KUNLUN_SESSION_PEPPER` | 随机生成 | 多实例/生产必须固定并通过 Secret Manager 注入 |
| `KUNLUN_PUBLIC_BASE_URL` | 空 | 公开站点的 HTTPS 根地址；支付 `return_url` 必须与它同源 |
| `KUNLUN_TRUSTED_PROXY_CIDRS` | 空 | 生产必须显式配置可信反向代理；内置 compose 只信任固定 Caddy `172.30.50.2/32`，其余来源的转发头被忽略 |
| `KUNLUN_TRUSTED_PROXY_SECRET` | 空 | Cloudflare Worker → Container 的共享代理密钥；至少 32 个可打印 ASCII 字符，只能通过 Worker Secret 注入，应用验证后会删除内部请求头 |
| `KUNLUN_OPERATOR_SIGNING_SECRET` | 空 | 生产运维短令牌签名密钥；通过 `scripts/mint_ops_token.py` 颁发最小 scope、短 TTL Token |
| `KUNLUN_PROVIDERS_JSON` | `[]` | 供应商名称、base_url、api_key_env、models、超时；不放密钥正文 |
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

供应商配置示例（只表示引用环境变量名）：

```json
[
  {"name":"provider-a","base_url":"https://api.example-a.com/v1","api_key_env":"KUNLUN_PROVIDER_A_KEY","models":["model-a"],"pricing":{"model-a":{"input_microusd_per_million":1200000,"output_microusd_per_million":3600000}},"connect_timeout_seconds":5,"read_timeout_seconds":60},
  {"name":"provider-b","base_url":"https://api.example-b.com/v1","api_key_env":"KUNLUN_PROVIDER_B_KEY","models":["model-a"],"pricing":{"model-a":{"input_microusd_per_million":1300000,"output_microusd_per_million":3900000}},"connect_timeout_seconds":5,"read_timeout_seconds":60}
]
```

## 预算、成本与故障切换边界

- 预授权、上游调用、结算是三个独立阶段；不会把上游网络调用放进数据库事务。
- 钱包和预算使用整数余额；账本和带明确 `target_type/target_id` 的运维审计只追加。SQLite 由防御性触发器禁止 UPDATE/DELETE，PostgreSQL 迁移同时禁止 UPDATE/DELETE/TRUNCATE，runtime 角色只有 SELECT/INSERT；生产预检会验证实际权限和守卫。
- 替换本月预算会继承本月已发生支出；新上限不能低于已发生支出。有未结算预授权时拒绝替换，预算替换与新预授权通过客户钱包行串行化，避免旧预算继续承接新请求。
- 允许切换：上游尚未接收请求的连接失败，以及明确拒绝且不计费的 429。其他错误只有在供应商书面计费契约和适配器测试证明“不可能计费”后才能显式放行。
- 禁止自动切换：500/502/503、读/写超时、参数/认证/权限等 4xx、内容审核拒绝、响应解析失败、客户端断开、已经向客户端发出流式首字节及其他不确定状态。5xx 默认按“可能已受理并计费”进入人工对账，避免两个供应商重复执行和双份成本。
- 每次请求记录 request ID、供应商尝试顺序、模型、Token、估算标记、预授权/实际成本、失败类别和最终供应商；不记录提示词、回答、请求正文或正文哈希。
- 开启内容安全时，输入检查对象与真正发给供应商的规范化 payload 一致，覆盖 `messages`、`tools`、`tool_choice`、`response_format` 等字段；任何检查失败都发生在预授权和上游调用之前。
- `/ops/reconciliation`、退款、风险处置与指标接口均需要独立运维短令牌和最小 scope。生产不使用旧的静态 `KUNLUN_OPERATOR_TOKEN` 兼容路径。人工释放必须确认上游未计费；人工结算必须提交已核对的输入/输出 Token 与上游成本，并生成带目标对象的不可变运维动作记录。
- checkout 先以数据库 CAS 取得唯一五分钟调用租约，并把客户 `Idempotency-Key` 透传给 sidecar；并发请求只有一个能调用外部支付。调用超时或租约过期不盲目重试，只能按商户订单号人工查询/对账。退款命令同样先持久化后调用支付桥，进程崩溃可由同一幂等键租约重领或认证 webhook 完成。
- 同一用户的支付入账和退款最终化都锁定钱包行。现金已退但可用额度不足时会扣尽剩余额度、记录差额风险并冻结账户；在途预授权全部结算/释放后，持有 `payments:risk:write` 的财务操作员才可选择全额追回或显式计入 `PLATFORM_LOSS`。处置不会自动解冻，存在其他风险或邮箱未验证时仍禁止解冻。
- checkout 在触发支付 bridge 前执行用户/IP 限流和单用户未关闭订单上限；过期调用租约只进入查询/对账，不会静默新建第二个支付意图。`payment.closed` 会终结未支付订单并释放名额。
- 数据库限流窗口由独立 maintenance 进程每 60 秒删除七天前的计数，同时恢复超时模型预占到 `pending_reconciliation`；业务 API 不承担清理循环，这不替代公网边缘限流。
- 当前限流是数据库固定窗口实现。多副本生产仍应在 Redis/WAF/API Gateway 增加边缘限流与熔断，并保留 PostgreSQL 资金事务和监控。

## 已实现、待验证与不做事项

已实现（代码与本地验证范围）：注册/登录、邮件验证/密码恢复服务、API Key 摘要保存与吊销、Turnstile hostname/action 服务端绑定、可信代理 IP 边界、测试 HMAC 订单回调幂等、现金支付与服务额度分离的整数双式账本、余额/预算预授权与结算、OpenAI-compatible 非流式与 SSE 流式接口、工具调用字段透传及完整输入安全检查、流式断线/5xx 待对账、供应商受限故障切换、成本台账、输入输出内容安全流式响应上限、运维短令牌、checkout 外部副作用幂等租约与防滥用上限、支付/退款可发现对账队列与持久化认领租约、可恢复退款及财务风险处置、审计目标与数据库不可变守卫、限流数据保留任务、Alembic `0001→0010` 迁移链及敏感正文不落日志的测试覆盖。本机原生 PostgreSQL 已通过迁移、runtime 权限正/负测、审计篡改负测、不同订单并发退款、同订单并发 checkout claim 和账本平衡验证；这仍不是持续压测或真实商户证据。

待验证（外部证据尚未提供）：真实官方供应商联调、各供应商 5xx/usage/计费契约、PostgreSQL 持续并发压测、Redis/边缘限流、正式支付 SDK/证书/商户与小额支付退款对账、SMTP 送达与域名 SPF/DKIM/DMARC、真实域名绑定的 Turnstile site key/secret 与浏览器到服务端二次校验、内容安全和投诉流程、TLS/WAF/KMS、备份恢复演练、真实客户验收以及公网灰度。浏览器 Turnstile 组件及服务端 Siteverify 适配已经实现，但没有真实密钥与域名验收就不算上线证据。

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

## GitHub → Cloudflare Containers

本仓库包含 `wrangler.jsonc` 和边缘 Worker，可由 Cloudflare Workers Builds 从 GitHub 的 `main` 分支构建 Dockerfile 并发布。该路径依赖 Cloudflare Workers Paid、外部托管 PostgreSQL、Cloudflare 账户授权和运行时 Secrets；Cloudflare Pages 不能替代这个 FastAPI + PostgreSQL 后端。

边缘 Worker 固定执行以下安全策略：

- 公网 `/ops`、`/ops/*` 和 `/metrics` 始终返回 404，包括常见 URL 编码绕过；
- 覆盖客户提交的代理头，并使用 `KUNLUN_TRUSTED_PROXY_SECRET` 认证 Worker → Container 的客户端 IP；
- 公开注册、测试支付、真实支付和真实模型上游固定关闭；当前 Cloudflare 配置只用于受控 staging，不是匿名公共 API 充值站；
- 一个固定 Container 实例承载 API，另一个固定实例由 Cron Trigger 每五分钟执行一次 maintenance；不会把容器临时磁盘当数据库；
- 缺少 PostgreSQL URL 或任一核心 Secret 时，Worker 返回不含敏感值的 503，不启动业务 Container。

首次连接前，先在独立受控环境完成数据库步骤：

1. 创建外部 PostgreSQL，并建立不同的 `kunlun_migrator` 与 `kunlun_runtime` 角色；连接必须使用 TLS。
2. 用 migrator URL 执行 `python -m alembic upgrade head`。
3. 用 runtime URL 与独立 migrator URL 执行 `python -m scripts.preflight`，确认 schema 精确位于当前 head 且 runtime 无迁移、历史账本修改或审计篡改权限。
4. 只有预检通过后，才向 Cloudflare 配置下列 Worker Secrets：

```text
KUNLUN_DATABASE_URL=postgresql+psycopg://<runtime role>@<external host>/<database>?sslmode=verify-full&sslrootcert=/app/certs/supabase-prod-ca-2021.crt
KUNLUN_API_KEY_PEPPER=<32+ random printable ASCII characters>
KUNLUN_SESSION_PEPPER=<different 32+ random printable ASCII characters>
KUNLUN_TRUSTED_PROXY_SECRET=<different 32+ random printable ASCII characters>
```

不要把 migrator URL 注入长期运行的 Worker/Container，也不要把任何真实值写进 `wrangler.jsonc`、GitHub Actions 或仓库文件。
`0008` 之后的安全迁移要求数据库实际登录角色名精确为 `kunlun_migrator`；其 downgrade 保持权限收紧，不会重新授予 Supabase Data API 角色。生产降级只能在维护窗口、显式破坏性确认和已验证备份下执行。

Supabase 部署镜像内置其公开的 `Supabase Root 2021 CA`，运行时使用 `verify-full` 同时验证 CA 与数据库主机名；证书 SHA-256 指纹为 `80:70:25:AD:50:D4:ED:21:9D:2C:9C:7D:29:9C:00:4F:82:4E:B0:0C:F7:F6:5A:FE:F6:07:D0:7B:72:E6:CA:FA`，有效期至 2031-04-26。替换证书前必须从 Supabase 官方控制台重新下载并复核指纹。

在 Cloudflare 控制台安装 **Cloudflare Workers and Pages** GitHub App 时，只授权本仓库；选择 `main` 为生产分支，构建命令使用 `npm ci && npm run check`，部署命令使用 `npm run deploy`，根目录为仓库根目录。Cloudflare Workers Builds 的生产部署会构建 Dockerfile；非生产分支的 `versions upload` 不会发布 Container 镜像，不能当作完整预览环境。

本地可在已登录 Cloudflare 且 Docker 可用时执行：

```bash
npm ci
npm run check
npx wrangler deploy
```

如果本机没有 Docker，只能用 `npx wrangler deploy --dry-run --containers-rollout=none` 检查 Worker bundle；这不构建 Container，也不是部署证据。首次部署后还必须实测 `/healthz`、`/readyz`、受保护接口、编码后的私有路径、Container 休眠重启、Cron maintenance、账本持久性和数据库恢复。Workers Builds、Secrets、域名、WAF 与 PostgreSQL 均属账户侧配置，单纯推送 GitHub 不代表 Cloudflare 已上线。

## 部署、迁移与回滚

生产使用 Alembic 迁移链：`migrate` 服务以 `kunlun_migrator` 执行 `python -m alembic upgrade head`，API 只用 `kunlun_runtime` 运行且不调用 `create_all`。发布前必须先备份并完成恢复演练；禁止直接在生产删除账本或钱包记录。迁移 head 必须与代码要求精确一致。

隔离 PostgreSQL 验证库可执行 `KUNLUN_CONFIRM_TEST_DATABASE=YES_ISOLATED_TEST_DATABASE KUNLUN_TEST_POSTGRES_URL=... python -m scripts.verify_postgres_concurrency`，复核当前 head、同用户不同订单并发退款、checkout claim 排他性、钱包/账本一致和冻结风险路径。该脚本会写入验证用户、订单、退款和不可改写账本行；未设置显式确认值时会在任何写入前拒绝执行，严禁指向生产库。

推荐发布顺序：备份数据库与配置 → 部署候选镜像 → 运行 `/healthz`、`/readyz` 和只读账本检查 → 小流量灰度 → 观察上游失败率、pending reconciliation、余额对账差异 → 扩大流量。回滚时切回上一镜像并保留数据库，不回滚/改写已入账交易；未完成请求进入人工对账队列。任何数据库 schema 变更必须先做可恢复备份并提供向前/向后兼容方案。

## 合规参考（上线前请由专业人士复核）

- 国家网信办《生成式人工智能服务管理暂行办法》：<https://www.cac.gov.cn/2023-07/13/c_1690898327029107.htm>
- 国家网信办关于开展生成式人工智能服务备案工作的公告：<https://www.cac.gov.cn/2024-04/02/c_1713729983803145.htm>
- 国务院关于非银行支付机构监督管理条例：<https://app.www.gov.cn/govdata/gov/202312/17/510339/article.html>
- 工业和信息化部互联网信息服务管理相关规定入口：<https://sdca.miit.gov.cn/zwgk/fgbz/art/2026/art_fea940f81f1d423e87101adf147ab979.html>
