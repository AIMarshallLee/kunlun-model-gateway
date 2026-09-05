# BYOK 交付候选版验证记录

验证日期：2026-09-04（America/Los_Angeles）。范围为本仓库 `codex/byok-gateway-completion` 分支的交付候选代码，不表示已经部署、供应商授权、真实付费或客户验收。

## 已验证

- Python 全量测试：360 passed，1 条既有 TestClient 弃用警告；分支覆盖率 83%，通过项目 80% 门槛。
- `npm run check`：TypeScript 检查通过，Worker/OpenCode 幂等插件测试 37 passed。
- `node --check app/static/app.js` 与 `git diff --check`：通过。
- PostgreSQL 16 全新隔离数据库：从空库迁移至 `0013_byok_budget_reconciliation`；runtime/migrator/executor 权限、安装标记、Data API 禁用与追加型账本守卫通过。模拟 Vault 完成存储、解析、跨租户/错误供应商/旧版本拒绝、轮换、吊销；故意添加角色继承边时 bootstrap 正确拒绝。
- 本地真实浏览器 + SQLite + 内存 Vault + 模拟供应商：一次性链接激活、登录、保存模型连接、设置预算、生成 Key、提交固定测试、成本更新、按原任务查询、逐次处理记录、断开连接均通过；刷新退出会话，注册/充值/余额面板在 BYOK 下不显示，官方 Key 提交后清空。模拟调用记录为 4 输入/3 输出 Token、7 microUSD，不是真实供应商计费。
- 高置信凭据模式扫描：本轮运行代码、脚本、迁移、文档和模板没有匹配的真实 Key/私钥模式；此检查不替代人工敏感信息审查或完整密钥检测服务。

## 尚未验证，不能包装成完成

- 目标账户中的 Vercel 部署、域名/WAF/入口保护、Cron、冷启动和生产重部署持久性。
- 真实 Supabase Vault 加密、生产三角色配置、备份恢复和真实供应商轮换/吊销。
- 真实官方 API 调用、成本目录与供应商账单核对、OpenCode 实际客户环境验收。
- 完整公众号业务层、付费客户、复购、毛利、市场规模与持续收入。

GitHub CI 与镜像构建结果请以对应提交/PR 的实时检查为准；不能用本文件替代外部检查结果。上线与回滚步骤见 [客户交付手册](CUSTOMER-DELIVERY.md)。
