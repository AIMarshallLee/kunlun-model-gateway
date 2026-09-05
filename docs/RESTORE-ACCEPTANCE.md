# 备份恢复验收：隔离自动化与真实 Supabase 边界

本页对应 `0017_chargeback_returns`。新增脚本只处理显式确认的本地一次性 PostgreSQL CI 库；不是生产备份服务或真实 Supabase 恢复工具。

## 1. 已实现的隔离检查

GitHub 的 PostgreSQL 16 job 先运行 `scripts/ci_postgres_gate.sh`，再运行：

```sh
.venv/bin/python scripts/verify_restore_postgres.py
```

环境沿用该 CI job 的纯合成凭据，必须满足：

- `KUNLUN_CI_ISOLATED_DATABASE=kunlun-ci-disposable`；`PGHOST=127.0.0.1`；`POSTGRES_DB=kunlun_ci`；`POSTGRES_USER=postgres`。
- `PGPORT` 为隔离实例实际端口；四个角色密码来自测试环境，不要填写生产凭据。
- 不接受 `PGSERVICE`、`PGSERVICEFILE`、`PGHOSTADDR`、`PGOPTIONS` 覆盖连接目标。
- `pg_dump`、`pg_restore`、`createdb` 客户端与 CI PostgreSQL 16 配套；本机旧版客户端不能导出新版本服务器。不要因此升级生产数据库。
- 源库没有 API、维护任务或其他写入者；源快照、导出、恢复期间必须保持静止。

脚本会先拒绝已经存在的 `kunlun_restore_ci`，然后在源测试库生成一笔合成的未知费用请求，并保持其客户及平台预算占用。导出使用 custom archive，在权限为 `0700` 的临时目录创建 `0600` 文件；保留 owner 与 ACL。只向新建的 `template0` 空库恢复，以单事务和遇错停止执行，不使用 `--clean`、`--no-owner` 或 `--no-acl`。

恢复后必须全部通过：

| 检查 | 失败标准 |
| --- | --- |
| 数据完整性 | `public`、`kunlun_private`、合成 `vault` 的表集合或任意行与导出前不同；比较覆盖空表，不输出行或内容指纹 |
| 真实迁移链 | 不是代码要求的精确 Alembic head |
| 角色与隔离 | runtime 权限、Data API RLS、Vault executor 边界、安装 ID 契约任一失败 |
| 不可变记录 | 账本／运维／凭据审计触发器及权限契约任一失败 |
| 财务状态 | 双式账本不平，或未知费用请求不再待对账、attempt 不再 unknown、客户／平台占用丢失 |

成功只输出表数、合成行数、演练耗时和检查范围；耗时不是生产 RTO 承诺。临时 archive 自动删除，新测试目标库保留供检查，由一次性 CI 服务结束时销毁。失败不清理或覆盖目标库；再次运行会拒绝已有目标。要重跑应创建另一套一次性测试集群，不要把 `DROP DATABASE` 加入生产手册。

## 2. 旧入口已停用

`scripts/backup_postgres.sh` 与 `scripts/restore_postgres.sh` 都返回退出码 2，并指向本页。它们不会调用 Docker、创建／改写备份或访问数据库，即使传入旧 `YES_RESTORE_PRODUCTION` 确认值也不会运行。

原因：当前生产 Compose 使用外部 Supabase，没有旧脚本假定的 `postgres` 服务；旧导出丢弃 owner／ACL，旧恢复包含覆盖清理参数，不能作为当前系统的恢复证明。不要恢复旧脚本或把旧确认字符串视为一次新的生产授权。

## 3. 真实 Supabase 恢复：仍需批准后执行

Supabase 官方说明，Vault 数据导出保留密文；项目加密密钥与数据库行不是同一份备份。手工跨项目 `pg_dump`／`pg_restore` 不会自动让新项目解密旧密文。官方“恢复到新项目”路径另有加密根密钥处理，不能把普通 PostgreSQL fixture 等同于它。[Vault 官方说明](https://supabase.com/docs/guides/database/vault)、[恢复到新项目](https://supabase.com/docs/guides/platform/clone-project)。

真实演练必须另行确认源／目标项目、恢复方式、费用、维护窗口、操作人和批准记录。最低步骤：

1. 暂停新注册／购买／模型调用及相关维护写入；记录备份时点、代码／迁移版本、订单和在途请求水位。恢复旧时点可能遗失其后的支付与调用状态，不能直接重开收费。
2. 使用适用于该项目的官方备份／恢复路径；确认备份保留和恢复时点，不在未知目标上执行覆盖操作。[Supabase 备份说明](https://supabase.com/docs/guides/platform/backups)。
3. 核验三个数据库角色、登录权限、schema、安装 ID、RLS、不可变审计及 Vault 契约。跨集群角色不会由本次同集群演练证明；Supabase 管理角色不可直接照搬普通 superuser 恢复命令。
4. 按所选官方流程恢复加密依赖，并在受控环境验证 Vault 真实解密、凭据版本与撤销状态；不把明文或根密钥写入报告、聊天或 Git。备份可能包含曾被撤销的凭据状态，恢复后必须与当前撤销记录核对。
5. 通过 `python -m scripts.preflight --require-managed-launch` 及受保护 readiness；分别恢复受控环境中的 peppers、运维签名密钥、支付验证配置、邮件和域名配置。它们不是 `pg_dump` 的完整覆盖对象。
6. 对账备份时点之后的支付、退款、拒付和供应商消费；未知状态保持占用和人工处置，禁止自动重发支付、模型调用或直接释放余额。验证现有幂等键和支付事件重放不重复记账。
7. 记录恢复耗时、允许的数据损失窗口、对账差异和修复证据；只在零未解释财务差异且负责人批准后恢复新业务流量。

任何一步失败都保持维护状态，保留证据，不能用“数据库能连接”代替业务恢复验收。生产 RPO／RTO 目标、项目备份可用性、真实密钥恢复及真实支付／调用联调目前均待验证。

## 4. 回滚与证据

本批不改业务 schema，不修改任何生产数据／密钥或云资源。撤回 CI 检查不需要数据库降级，但不可因此重新启用旧覆盖恢复入口。报告只保留提交 SHA、CI 链接、PG 版本、表／行计数、检查结果和耗时，不上传数据库 archive。

PostgreSQL 的 custom archive、owner／ACL 和单事务恢复语义参见 [PostgreSQL 16 pg_restore 官方文档](https://www.postgresql.org/docs/16/app-pgrestore.html)。本项目选择新空目标、不清理已有对象；不提供可直接粘贴到生产的覆盖命令。
