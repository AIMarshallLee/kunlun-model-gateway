# 商业模式部署准备与检查

适用：商业 PRD P0-12；主产品仍是客户注册、购买调用额度、使用本站 Key 的商业模型网关。本文是可验证的部署准备，不是生产发布授权，也不是正式支付 SDK、商户或供应商已经接通的声明。

## 本批交付

| 文件 | 用途 |
|---|---|
| `.env.managed.vercel.example` | 首选 Vercel 路径的商业配置字段；不含 migrator 连接或任何模型 Key |
| `.env.managed.compose.example` | 经批准采用自托管时的商业配置字段；三角色连接按服务隔离 |
| `docker-compose.managed.yml` | 叠加既有 production Compose，只补 API／一次性 preflight 的商业配置 |
| `scripts/preflight.py` | 默认仍执行数据库检查；新增 `--config-only` 和 `--require-managed-launch` |

原 `.env.production.example`、`.env.vercel.production.example` 和 production Compose 基线保持 BYOK 兼容。不要把新旧模式的值拼接后不做检查，也不要为了通过而改成 `legacy_test`。

## 默认状态与需补齐的输入

新模板固定 `production + managed_gateway + supabase_vault`，但注册、收款、上游开关全部为 false，日成本上限为 0，供应商／合规／私有入口／官方 SDK 确认全部为 false。因此原样使用应失败，而不是自动开站。

| 输入组 | 完成要求 |
|---|---|
| 账号与经营依据 | 明确主体、允许市场、支付渠道／商户、供应商用途依据；确认字段只是操作员声明，不是授权证据 |
| 数据库 | runtime、migrator、Vault executor 指向同一安装，独立角色和密码；verify-full TLS，可读的绝对 CA 路径；schema 精确匹配代码 head |
| 模型与价格 | 真实获准的固定允许目录、模型 ID、输入／输出实际成本、独立售卖价、批准的 UTC 日成本上限；不把测试模型或示例金额上线 |
| 身份与支持 | 持久化 peppers、SMTP、发信地址、HTTPS 公共域名、匹配域名的 Turnstile、服务条款／隐私／投诉入口；实测验证及密码恢复 |
| 内容安全 | 获准的安全服务 URL、Key、允许主机和策略版本；不是默认放行，也不宣称供应商不保留内容 |
| 支付 | 正式 SDK／sidecar、商户、bridge 地址及独立密钥、允许主机、SKU；本仓库现有 bridge 协议与布尔确认不替代正式 SDK 实现 |
| 运维入口 | 真实 IdP/MFA 与受保护入口，短时分权限运维令牌；模型 Key 只经平台 Vault 管理，不进入模板 |

不在聊天、Git、工单正文、截图或命令行参数中提交秘密。现有 peppers 不能当普通随机配置随意更换；会影响凭证校验。真实密钥变更、数据库迁移、收费和生产发布仍须单独批准。

## 两级检查命令

命令只读取进程环境，不自动加载 `.env`、登录账号、生成密钥或修改开关。通过批准的秘密注入方式提供环境；不要把含秘密的完整环境打印到日志。

第一步只检查本地配置，不连接数据库、支付、邮件、验证码或模型服务：

```bash
python -m scripts.preflight --config-only --require-managed-launch
```

`--require-managed-launch` 要求商业模式以及注册、正式支付、上游三个开关齐备；其余安全配置复用 `Settings` 校验。缺配置退出 1，只报脱敏错误。不会自动设置 true，也不会跳过商户／供应商的人工审批。

静态检查仍需三角色 URL，用于核对角色名、TLS 参数、不同密码和同一 Supabase project/database；它也检查本地 CA 可读性。Vercel 应用环境只放 runtime／executor，migrator 在独立的一次性运维检查环境追加。容器示例 CA 为 `/app/certs/supabase-prod-ca-2021.crt`；在宿主机检查时使用该机器上真实、可读的 CA 绝对路径，不能因此关掉 TLS 验证。

配置检查通过只输出“配置静态检查通过”“未连接数据库”“不等于商业上线”。它不验证数据库是否存在、真实权限、Vault Key、远端服务协议或连通性，不能代替后续检查。

第二步在批准的目标与受控环境中执行完整技术预检：

```bash
python -m scripts.preflight --require-managed-launch
```

此命令不执行迁移或交易，但会连接三角色数据库，检查精确 schema、账本／审计不可变性、RLS／ACL、客户与平台 Vault 契约以及同一安装标记。默认不传 `--config-only`，不会因为增加新选项而省略原有数据库门禁。安装后的入口 `kunlun-production-preflight` 支持相同选项；CI 在无网络容器中核验其 `--help`。

## 两条部署路径的边界

### Vercel（首选）

以独立商业模板为字段清单；运行连接和 Vault executor 连接放应用受控环境，migrator 不进入持续运行的应用。沿用现有 ingress／cron 适配器及 Dockerfile，不更改当前项目或部署账号。

当前应用配置明确禁止在 Vercel Preview/Development 开启 managed/BYOK；`staging` 环境也不能靠开启真实收款／上游绕过限制。受保护的商业候选验收需要另行批准隔离目标及其生产安全配置，不等于开放客户访问。不得为跑预览而篡改 `VERCEL_ENV`。

本批未验证 Vercel 当前账号资格、地域、流式时长、请求体／并发、数据库连接池或 cron 实际调度。镜像构建与健康检查通过不证明这些实网能力满足 PRD；如果目标环境不支持，须提交具体失败证据后确定最小托管调整，不能默默改平台。

### Compose（保留的自托管路径）

商业覆盖文件只与既有 production 文件一起使用。填好的环境文件建议命名 `.env.managed.compose`（Git 忽略）；配置渲染用安静模式，避免把秘密展开到终端：

```bash
docker compose --env-file .env.managed.compose \
  -f docker-compose.production.yml -f docker-compose.managed.yml config --quiet
```

这一步只渲染配置，不构建、不迁移、不启动、不付款。空模板渲染失败是预期行为。不要用真实环境运行 `config --format json` 并把结果粘贴到报告；测试中的 JSON 渲染只含合成值。

实际启动仍会沿用 `migrate → preflight → API/maintenance → Caddy` 依赖，故 `up` 属于可能执行真实迁移和发布的操作，本批没有执行。不要把 `docker compose run preflight` 当成无副作用检查：Compose 依赖可能启动 migrate。若已批准独立检查且迁移已完成，需使用 `run --rm --no-deps preflight` 并复核目标。

API 只接 runtime／executor 和所需业务服务凭据；preflight 一次性持有三角色连接；migrate 只接自己的数据库 URL；maintenance 不接 Vault、支付、邮件、安全服务秘密；Caddy 只接域名与证书邮箱。维护服务仍处理过期计数及遗留占用，不执行自动支付退款或不确定调用重试。

Caddy 仍阻断公开 `/ops*` 和 `/metrics`；本批不放开管理员入口。告警邮件 worker 仍需独立批准的收件人和调度，不因 Compose 启动而自动发信。

## 验收证据与仍需完成的发布门槛

本批测试验证模板关闭默认值、填充合成配置的可解析性、模式／开关错配拒绝、三角色目标及密码隔离、错误脱敏、静态检查零数据库连接、完整检查仍有 schema 门禁，以及实际 Compose 合并后的服务级配置隔离。测试没有连接真实商户或供应商，也没有生产数据库读写。

本地结果：新增 11 项部署契约测试通过；全量 520 项 Python 测试通过、分支覆盖率 84%；49 项 TypeScript 测试与类型检查通过。`uv lock --check --offline` 通过，无依赖升级或锁文件改动。保留一项既有 Starlette/httpx 弃用警告。GitHub CI 以对应提交的实际结果为准。

完成商业发布仍需：正式支付 SDK、获准的真实供给与计费维度、管理入口强认证、客户完整浏览器流程、受保护实网验收、真实小额支付／调用／退款、备份恢复与容量／重部署验证。

特别注意：当前公开目录的 `purchasing_enabled` 主要反映收款配置开关与 bridge 对象，不证明 Vault 中已有可用供给；`/readyz` 的配置／Vault 契约检查也不等于真实模型可调用。后续需补供给就绪与购买入口的一致性门禁，不能仅凭这些字段开放收款。

回滚本批模板／检查工具不会撤回任何已经执行的迁移或凭据变化。本批无 schema 变更，head 仍为 `0017_chargeback_returns`。真实环境故障优先停止新流量、保留在途占用和全部账本／审计；禁止删账、恢复旧 Key 或直接降级数据库冒充回滚。
