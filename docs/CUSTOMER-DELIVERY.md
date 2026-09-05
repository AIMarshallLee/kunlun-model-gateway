# BYOK 客户交付与上线验收

本项目卖的是模型连接、支出控制、异常处理与持续运维，不是低价 Token。当前代码是候选版本；本地测试、GitHub CI、真实生产验收和付费客户验证是四件不同的事。不能承诺复制几个账号就必然上线。

## 1. 已实现与边界

已实现：私有邀请/恢复、客户密码与会话、客户独立 API Key、模型连接/轮换/断开、供应商目录、预算预授权、逐次 attempt 成本与最终响应分账、受限故障切换、用量报告、任务状态查询、OpenCode 配置安装器、生产预检与迁移。

没有实现的业务层：公众号品牌资料、选题、文章审核、草稿交付及发布。本仓库是后台网关和客户控制台，不能把它叫做完整公众号 SaaS。人工服务报价、客户合同、收款和续费由你现有业务渠道处理，没有自动订阅计费。

生产不做：公共注册、匿名充值、共享余额、默认共享上游 Key 兜底、无限量、自动发布。旧支付代码只能在隔离的本地兼容模式测试。

数据边界：网关不保存提示词、回答或正文哈希。连接密钥保存在 Supabase Vault；邮箱、密钥摘要、任务 ID、模型、Token、成本与审计元数据持久化。BYOK 不等于数据不出境，也不改变供应商自己的日志/保留政策。OpenCode 和业务产品可能自行保存内容，需单独配置和向客户披露。

## 2. 运营人员首次准备（不是每客户重做）

1. 确认目标 Git 提交、域名、Vercel 项目和 Supabase project；备份既有数据库并先演练恢复。没有生产变更批准，不执行迁移/部署。
2. 通过受控管理员流程建立 runtime、migrator、Vault executor 三个角色，使用三份独立密码；角色不得相互继承。管理员运行 `scripts/bootstrap_supabase_vault.sql`，migrator 运行 `python -m alembic upgrade head`。当前 head 为 `0013_byok_budget_reconciliation`。
3. 在受控预检环境同时提供三条数据库 URL，执行 `python -m scripts.preflight`。必须验证同 project、同 database、同安装 UUID、TLS verify-full、Vault ACL 和账本追加限制。CI 的模拟 Vault 只验证 SQL 权限，不能代替真实 Supabase 加密/轮换验收。
4. 将 `.env.vercel.production.example` 中的运行变量录入 Vercel 加密环境变量。仅录入 runtime 与 executor；禁止录入管理员或 migrator 凭据。Provider 目录填写允许的固定 HTTPS 地址、模型 ID 和已核验的官方价格，不填客户 Key。预览环境不可启用生产 BYOK。
5. 独立生成并持久化 API Key、会话、身份激活、运维签名、运维入口和 Cron 密钥。身份激活 Pepper 缺失会使开通失败；重部署不得重新随机生成这些值。不要通过聊天发送密钥。
6. 验收 TLS/WAF、运维入口双门禁、Cron、冷启动、流式响应、持久化、备份恢复与成本对账，再开放给被邀请客户。部署按钮成功不等于这些验收通过。

生产配置复杂的部分只做一次，但不能省略。平台套餐、可用区域和供应商价格由部署时的官方信息重新确认；本手册不承诺平台资格或固定费用。

## 3. 给一个客户开通

运营人员先通过既有业务渠道核验邮箱归属和身份。本版不自动发送邀请邮件；人工确认身份后签发链接，并且审计记录此次人工确认。不要把这叫作邮件系统完成了邮箱验证。

在可信终端签发只含 `accounts:invite` scope、短期有效的运维令牌：

```bash
python -m scripts.mint_ops_token --subject YOUR_OPERATOR_ID --scope accounts:invite --ttl 300
```

签名密钥由受控环境或 `KUNLUN_OPERATOR_SIGNING_SECRET_FILE` 注入，不作为命令行参数。禁止录屏、终端日志采集和把输出放进工单。

```bash
python -m scripts.customer_invite --origin https://YOUR_GATEWAY_DOMAIN \
  --email CUSTOMER_EMAIL --operation-id onboarding-UNIQUE_ID \
  --reason 'Identity confirmed through the agreed delivery channel' \
  --identity-confirmed --vercel
```

工具交互输入短期令牌和 Vercel 运维入口密钥，不回显、不跟随重定向、不自动重试。激活链接仅返回一次，1 小时有效，令牌放在 URL fragment；通过已核验客户渠道交付。链接过期，用 `--recover-user USER_ID` 替代 `--email`，并使用新 operation-id。恢复操作会废弃之前未用链接；客户完成重置后，旧会话和所有网关 API Key 失效。账户冻结时不可恢复。

网络断开或 HTTP 409：先用原 operation-id 查询受控审计，确认账户状态；不要不断点击开通。数据库核查应只读取账户 ID、状态和审计元数据，不导出密码摘要、客户密钥或 Vault 内容。

## 4. 客户只需走这条路径

1. 打开激活链接，设置至少 12 位密码，再登录。刷新页面需要重新登录，不在浏览器持久化会话令牌。
2. 在“连接模型账号”选择允许的供应商并输入自己的官方 API Key；连接保存仅证明安全存储成功，不代表模型权限已通过。
3. 设置月度供应商支出预算。界面及接口以 microUSD 计量，1 美元 = 1,000,000 microUSD；例如 5 美元填 5,000,000。新预算不会抹掉已发生支出或在途预授权。
4. 生成网关 API Key，在可信密码管理器保存；它不是官方模型 Key，两者不可混用。网关 Key 只显示一次。
5. 用“首次调用验收”输入网关 Key，显式运行一项最多 16 输出 Token 的测试。这是真实调用时可能计费的动作，不是免费连接检测。
6. 检查响应、任务 ID、实际供应商、Token、成本和预算占用；再配置 OpenCode。

```bash
python -m scripts.opencode_install --target /ABSOLUTE/PROJECT/opencode.json \
  --base-url https://YOUR_GATEWAY_DOMAIN/v1 --model YOUR_ALLOWED_MODEL
```

确认预览差异后，添加 `--apply` 写入。安装器保留原配置并生成备份，同时安装会话/assistant turn 幂等插件；不会写 API Key。通过可信环境注入 `KUNLUN_GATEWAY_API_KEY`。既有插件若内容不同，安装器拒绝覆盖，请先人工审查。

OpenCode 适配基于 v1.18.21 的事件契约；升级 OpenCode 后重新跑插件测试和真实小额任务，不承诺任意版本兼容。

## 5. 异常与售后操作

| 情况 | 系统行为 | 人工动作 |
|---|---|---|
| 预算不足 | 新调用被拒绝，不发给上游 | 检查支出和预授权；只有客户明确同意后才调整预算 |
| 明确连接失败或 429 | 仅切换到客户已连接且允许的后备供应商 | 核查 attempt，不启用共享 Key 兜底 |
| 认证、参数、审核拒绝、5xx 或不确定读取超时 | 停止自动切换 | 修复原因；可能计费的调用转人工对账 |
| 已开始流式响应后中断 | 不切换、不自动重放 | 核查任务，保留已有内容；客户明确决定是否开新任务 |
| `pending_reconciliation` | 保留预授权，不把估算当最终账单 | 对照供应商真实用量；通过受控对账接口结算/释放，不直接改账本 |
| `request_already_recorded` / 409 | 返回原任务 ID，不再次外呼 | 查询原任务；网关不存回答，不能恢复已丢失回答正文 |
| `revoked_pending_destroy` | 连接立即停用，物理密钥清理待完成 | 重试断开/清理，直到彻底吊销；必要时客户在供应商后台撤销 Key |

查询原任务：API Key 调用 `POST /v1/requests/lookup`，携带原 `Idempotency-Key`；也可调用 `GET /v1/requests/{request_id}`。控制台通过“查询上次任务”或任务记录查看。404 不能证明在途任务未被接受。不要自动生成新的幂等键规避冲突。

预算只约束经过本网关的请求，无法约束客户在其他地方使用同一官方 Key 的支出。价格目录、Token 上界和上游实际用量存在差异时会进入异常处理；不是供应商账单的绝对保额承诺。建议在供应商侧同时设置支出控制，并用独立项目 Key。

售后包含：约定工作时间内连接排查、预算说明、版本升级与回滚协助、异常对账。默认不包含：24/7 SLA、内容正确性担保、供应商封号赔偿、代购账号、文章人工编辑、公众号发布和供应商账单退款。响应时间和人工服务另行书面约定。

## 6. 每客户交付包与通过标准

- 交付包：站点地址、允许的供应商/模型清单、一次性激活链接（单独保密交付）、预算确认单、OpenCode 配置及备份、脱敏验收记录、售后联系人/时间、升级回滚说明。禁止打包客户 Key。
- 通过：客户独立激活登录；供应商连接；预算拒绝不外呼；一项非流式与一项流式真实小额任务；同幂等键不重复计费；逐 attempt 与最终响应可解释；跨客户访问拒绝；异常保留预授权；轮换和断开有效；重部署后记录仍在。
- 记录：任务成功率、人工接管次数、每篇或每任务成本、修改次数、恢复时间、客户复购/续费意愿；市场规模、付费意向和收入没有证据时一律标记待验证。
- 停止交付：任一越权/密钥泄露、静默超支、无法解释的重复调用、Vault/备份恢复失败。先暂停新任务并处理，不以“之后修复”通过验收。
- 回滚：记录 Git commit 与镜像 digest；先关闭新 BYOK 调用并保留待对账项。数据库迁移不盲目 downgrade；仅回滚到与当前 schema 兼容的已验证应用版本，否则在维护窗口按验证过的备份恢复方案执行。代码回滚不自动等于数据库回滚。

## 7. 本地验证命令

```bash
uv sync --locked --extra postgres --extra test --python 3.12
uv run coverage run --branch -m pytest
uv run coverage report --fail-under=80
npm ci --ignore-scripts
npm run check
```

GitHub CI 另在全新隔离 PostgreSQL 16 中运行 `scripts/ci_postgres_gate.sh`；该脚本明确禁止用于真实业务库。首次真实生产验收仍需使用你的账户、受控测试预算和部署批准。
