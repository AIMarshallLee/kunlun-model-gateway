# 运营台：客户历史 Key 检索与分页

补齐 P0-10 已记录的“更多 Key 历史检索”缺口，不改变密钥签发、冻结、吊销或计费规则。没有生产迁移、真实账号操作或发布。

## 接口

沿用 `GET /ops/accounts/{user_id}`，仍要求短时运维 token 的 `accounts:read` scope。客户登录态、客户 API Key、仅 `console:read` 的 token 均不授予读取权限。

| 新参数 | 规则 |
| --- | --- |
| `key_limit` | 默认 100，范围 1–200；保留旧调用的默认最多 100 条行为 |
| `key_offset` | 默认 0，范围 0–1,000,000 |
| `key_id` | 可选精确 Key ID；1–64 位字母、数字、下划线或连字符，不接受完整 API Key 的点分格式 |

返回继续包含 `account`、`wallet` 和安全 Key 元数据；新增 `keys_pagination={limit,offset,total}`。`keys_truncated` 表示后面仍有记录。按创建时间倒序、ID 排序解决时间相同的顺序不确定性；过滤条件始终包含当前客户 ID。其他客户的 Key 不会因已知精确 ID 而出现在该客户结果中。

这是当前状态分页，不是冻结快照。期间新增 Key 可能改变偏移位置；实际操作仍核对精确 ID，并通过既有 `expected_status` 与审计契约提交。不存在的客户返回 404，存在客户但无匹配 Key 返回空结果。密钥正文、摘要和会话 token 不在投影字段内。

## 界面

客户详情默认每页 20 条，显示当前区间／总数、上一页／下一页、精确 ID 输入及显示全部。所有 Key 状态都可查；只有 active／frozen Key 提供既有的冻结／解冻动作，revoked Key 不可恢复。

翻页、重新检索和修改检索输入都会取消旧待确认命令。执行写请求期间禁用分页与筛选；退出或切换对象后，迟到结果不恢复上一对象数据。语言切换不发送请求或改变已冻结的命令。完整 API Key 格式在前端拦截，不作为 URL 查询发送；此字段只接收标识符。

样式沿用既有深色、等宽数据与绿色状态色，不引入第三方资源或浏览器存储。手机端表单垂直排列，分页按钮自动换行。

## 已执行的验证

- API 回归：206 个 Key 的三页完整遍历，无重复或遗漏；精确 ID、真实另一客户 Key 的排除、权限拒绝、无效分页边界和敏感字段排除。
- 浏览器：206 条／11 页，查询第 11 页的旧 Key；语言切换保持已准备命令；重查取消旧确认；明确确认后仅发出一次旧 Key 冻结，重新读取状态为 frozen。
- 浏览器：完整 Key 格式未出网、退出后迟到分页响应不恢复客户数据、桌面／390px 手机布局无横向溢出。
- 截图：忽略目录 `release-artifacts/ops-key-history-desktop.png`、`release-artifacts/ops-key-history-mobile.png`；全部是本地合成数据，不是客户案例。

复现须使用新鲜的一次性夹具：

```sh
.venv/bin/python tests-browser/ops_fixture.py --key-history
# 在另一个终端使用已有 Playwright/Chromium
node tests-browser/ops-key-history.cjs
.venv/bin/python -m pytest tests/test_ops_console.py -q
```

`--key-history` 只在测试目录种入合成历史数据，不进入正式应用。API 和完整运营台仍需目标 PostgreSQL／保护入口验收；这批不完成正式支付 SDK、真实商户或 IdP/MFA。

## 回滚

无 schema 变更，head 仍为 `0017_chargeback_returns`。前后端应作为同一版本发布／回退；撤回分页 UI 不会撤销已确认的 Key 冻结。不能通过改表、删除 Key 或删审计恢复旧状态。
