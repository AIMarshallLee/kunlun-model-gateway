# 生产运行手册（网关）

## 发布前

1. 准备独立的 PostgreSQL、持久化 API/session/identity pepper、运维签名密钥、SMTP、Turnstile CAPTCHA、内容安全、模型供应商和（若启用）支付 bridge 凭据。Turnstile 必须同时配置浏览器 site key、服务端 secret 和与 `KUNLUN_PUBLIC_BASE_URL` 一致的 expected hostname；服务端固定调用官方 Siteverify，并分别校验注册、重发验证、密码找回 action。凭据只放在受控 secret store 或 `.env.production`，不提交 Git。生产 API 使用 `kunlun_runtime`，一次性迁移服务使用不同的 `kunlun_migrator`；不要让 API 使用 PostgreSQL owner。
2. 在部署主机加载受控环境后执行 `set -a; . .env.production; set +a; python -m scripts.preflight`。它验证应用可观察到的配置、数据库实际登录角色、Alembic head，以及 runtime 对 schema、历史账本、运维审计和 `alembic_version` 的有效权限与不可变守卫；失败时保持公开注册、真实上游或真实支付关闭。生产真实上游还必须显式提供 `KUNLUN_MODELS_JSON`，且每个 Provider 对每个路由模型都给出完整的输入/输出上游价格。
3. 执行 `docker compose -f docker-compose.production.yml config`，确认仅暴露 Caddy 的 80/443；API、migrator 与 PostgreSQL 不直接暴露公网。内置部署固定 Caddy 为 `172.30.50.2`，应用只信任该 `/32` 覆盖写入的 `X-Kunlun-Client-IP`，Uvicorn 通用代理头解析保持关闭。若替换网络或反代，必须同步修改可信 CIDR，并做真实客户端 IP 与伪造头冒烟。再用 `docker compose -f docker-compose.production.yml run --rm migrate python -m alembic upgrade head` 验证迁移权限。
4. `docker compose -f docker-compose.production.yml up -d --build` 后检查 `/healthz`、`/readyz`、迁移 head、人工接管链路和日志脱敏，再开放 DNS。Caddy 的证书签发还需 DNS、80/443 入站和有效 ACME 邮箱的真实验证。

5. 仅在可丢弃的隔离验证库运行 `KUNLUN_CONFIRM_TEST_DATABASE=YES_ISOLATED_TEST_DATABASE KUNLUN_TEST_POSTGRES_URL=... python -m scripts.verify_postgres_concurrency`。脚本会写入验证用户、订单、退款和账本行；没有显式确认值时会在任何写入前拒绝执行。通过表示并发退款、checkout claim 和账本约束在该 PostgreSQL 实例上成立，不代表真实支付或持续负载已验收。

## 日常检查

独立 `maintenance` 服务每 60 秒运行一轮。它使用与 API 相同的
`kunlun_runtime` 数据库连接，恢复超过租约时间的模型预占到
`pending_reconciliation`，并仅删除 `rate_limit_counters` 和
`auth_rate_limit_counters` 中早于 7 天的分钟窗口；恰好位于 7 天边界的窗口
保留。手动执行一次可运行 `python -m scripts.maintenance --once`，清理或恢复失败时
由 Compose 重启维护进程，业务 API 不受影响。

- 观察 Prometheus 文本指标（仅记录计数、状态、耗时与供应商/模型的受控标识，不记录提示词、素材、密钥或完整响应）。
- `/healthz` 只表示进程存活；`/readyz` 表示数据库、迁移和关键外部依赖满足当前配置。
- 异常时先暂停公开注册/充值，确认预算硬停、失败切换、人工接管与台账状态，再处理供应商。
- 运维接口 `/ops/*` 与 `/metrics` 默认由 Caddy 返回 404；内部访问须走私网/管理入口，并携带 `scripts/mint_ops_token.py` 生成的短期、最小 scope Token。生产不使用旧的静态运维 Token。
- `PaymentRefund.status=risk` 表示现金已退但额度未完全追回；账户必须保持冻结。先通过模型对账把该账户所有 `reserved_microusd` 结算或释放，再由财务核对订单与不可变账本，使用最短有效期、仅含 `payments:risk:write` 的运维令牌调用 `POST /ops/refunds/{refund_id}/risk-disposition`。`recover_available` 只在可用额度足够全额追回时成功；`write_off` 会先追回现有额度，再把剩余差额显式记入 `PLATFORM_LOSS`。两者都要求唯一幂等键、理由和审计目标，且都不会自动解冻。之后只有在邮箱已验证且不存在其他未处置退款风险时，才可由另一个 `accounts:write` 审批动作解冻。
- 每个运维动作都必须有 `target_type`、`target_id`、操作人、scope、前后状态和理由；`operator_actions` 在 PostgreSQL 由触发器禁止 UPDATE/DELETE/TRUNCATE，runtime 角色不得持有这些权限。任何需要“修正审计”的情况只能新增更正记录，不能改写历史。

## 备份与恢复

当前生产数据库为外部 Supabase，旧 Compose 数据库备份／覆盖恢复脚本已停用，始终返回退出码 2。不要继续使用旧 `YES_RESTORE_PRODUCTION` 确认值或假定本项目包含 `postgres` 服务。

执行范围、隔离自动化和真实项目审批清单见[恢复验收](RESTORE-ACCEPTANCE.md)。CI 只在合成 PostgreSQL 库验证新空目标恢复、完整数据、预算占用、账本和权限；真实 Supabase 的备份时点、加密密钥、跨集群角色及恢复后支付／模型对账必须另行验证和批准。

## 事故边界

供应商超时、余额不足、验证码/内容安全不可用或支付回调验签失败时，系统应停止、降级或转人工；禁止静默重试造成超支。任何数据泄露迹象先隔离受影响租户和密钥、保留审计证据，再按内部通知流程升级。

本手册不证明域名、证书、商户、SMTP、模型账号或公网部署已经配置；这些必须由运营方在真实环境提供证据并复核。

## 上线状态矩阵

| 状态 | 允许范围 | 必须有的证据 |
| --- | --- | --- |
| 本地开发 | SQLite、测试支付、localhost | 本地测试结果；不得接入真实客户/商户 |
| 受控 staging | PostgreSQL + migrator/runtime 分离、关闭公开注册/真实支付 | 迁移、恢复、权限、故障切换和敏感日志检查 |
| 候选生产基线 | 生产配置与 `/readyz` 通过，功能开关仍可关闭 | 预检输出、镜像/迁移版本、备份恢复记录 |
| 公网灰度 | 真实域名/TLS/WAF、SMTP/CAPTCHA/内容安全、真实模型与支付沙盒或小额联调 | DNS/证书、邮件送达、支付回调/退款/对账、供应商与人工接管证据 |
| 公网正式 | 观察期和客户验收通过 | 真实商户/域名/PG/邮件/支付/备份证据、值班与合规材料 |

任何“代码测试全绿”都不能替代矩阵中列出的外部证据；缺失任一关键证据，保持相应功能关闭。
