# Text2SQL 节点级 Checkpoint 运行手册

## 已接通的恢复链路

`Text2SQLAgenticEngine` 的 `plan-first-text2sql-v3` 协议包含 11 个固定运行节点，每个节点成功后立即提交到独立 SQLite Store：

1. Lead 路由与委派；
2. Evidence Orchestration：stable 检索并构建 `SchemaLinkPack/v2`；
3. Plan Workers：Schema Grounding 与 schema-blind Query Planning 并发执行；
4. Harness 对 QuerySpec 与 SchemaPlan 做 deterministic bind；
5. Lead 对 BoundQueryPlan 做语义审核；
6. 必要时每个 Plan Worker 最多返工一次，重新 bind，由 Lead 做最终审批并由 Harness 铸造 ApprovedQueryPlan；
7. SQL Generation 从不可变 ApprovedQueryPlan 生成候选；
8. Harness 依次执行 `validate_sql`、plan conformance、`explain_sql`；如果零候选通过，允许一次 Generation 修复并重新执行这些门禁；
9. Blind Critic 审核通过门禁的匿名候选；
10. Lead 只能从 Critic 接受的已有候选中做最终选择；
11. Harness 重跑最终 validation、版本与计划一致性检查，然后只读执行。

模型角色只有 `text2sql-lead`、`schema-grounding`、`query-planning`、`sql-generation`、`text2sql-critic` 五个。Harness 是非 Agent、非 Skill 的应用执行主体。Query Planning 看不到物理 Schema；Schema Grounding 的 value binding 会先到固定的只读实库验证 physical value，再进入 deterministic bind。

并发只发生在第 3 个 `plan-workers` 节点内部；其余节点保持固定顺序。当前没有 Critic reject 后的 Generation 修复：第 8 节点的一次修复仅在进入 Critic 前、零候选通过确定性门禁时发生。

失败会保留已完成节点和累计 `ExecutionLedger`；普通运行异常还会记录失败节点的 attempt/error。进程重启后，相同身份的任务从首个未完成节点继续。Runtime 会先物化完整节点序列、拒绝重复节点名，并要求它与 checkpoint 绑定的顺序完全一致；只有 11 个节点都处于 `completed` 才能持久化最终结果。完成后的相同身份重复请求不会再次调用模型或执行 SQL。

Web 与 CLI 默认使用同一个可配置的存储位置：

```bash
EVOAGENT_TEXT2SQL_CHECKPOINT_STORE=artifacts/text2sql/checkpoints/runtime.sqlite3
```

CLI 已向 stable/candidate Engine 注入该 Runtime Checkpoint Store。恢复时必须复用同一个外部任务 ID；也可以用 `--checkpoint-store` 覆盖存储文件：

```bash
python scripts/run_text2sql.py "强烈岩爆案例有多少个" \
  --task-id request-0001 \
  --checkpoint-store artifacts/text2sql/checkpoints/runtime.sqlite3
```

`--checkpoint-store` 默认读取 `EVOAGENT_TEXT2SQL_CHECKPOINT_STORE`，未设置时落到 `artifacts/text2sql/checkpoints/runtime.sqlite3`。不传 `--task-id` 会生成新 ID，并在输出顶层 `task_id` 返回；要跨进程恢复，调用方应记录并在重试时显式传回该 ID。相同 task ID 还必须保持问题、principals、模型、预算和所有版本 pins 不变，否则身份校验会 fail closed。

CLI 与 Web 使用相同的 lane 隔离规则：

```text
<外部 task_id>:stable:<stable policy version>
<外部 task_id>:candidate:<candidate policy version>
```

外部 task ID 仍作为 release assignment 的 task key；上面两个内部 ID 只用于 Runtime Checkpoint。这样，同一次 shadow/canary 双跑的 stable 与 candidate 不会读取彼此节点，同一外部 ID 在 Policy 升级后也不会误读旧 Policy 的运行。若某次请求未命中 shadow/canary，只有 stable lane 会被创建。

Web 页面会在浏览器本地暂存未完成请求的 `task_id`；网络或服务中断后再次发送同一问题会复用该 ID，成功返回后清除。API 调用方也应在首次请求前生成并重试复用自己的 `task_id`。

Web 服务还在 Evolution Store 中保存一层请求记录：首次请求会冻结该次会话上下文与 Runtime 身份，完成后缓存对外响应；相同 `task_id` 的重试会复用冻结上下文，且 QueryTrace、会话消息和经验候选均按任务幂等写入，避免进程在“节点完成、响应未返回”之间退出时产生重复副作用。

## 身份与隔离

Checkpoint 身份绑定以下输入：

- 问题与会话 QueryRun 上下文的 SHA-256；
- principals（角色级 Tool ACL 的调用身份）；
- database snapshot、Wiki、Vanna、Memory、Policy 五项版本固定值；
- 模型 provider/model 与 temperature；
- `plan-first-text2sql-v3` 协议、`BUILD_VERSION=text2sql-agentic-build-v3`、`GATE_IMPLEMENTATION_VERSION=text2sql-harness-gates-v2` 和完整节点顺序；
- QuerySpec、SchemaPlan、BoundQueryPlan、ApprovedQueryPlan contract 列表；
- 最大候选数、每 Worker 最大计划返工次数和最大 SQL 修复次数；
- Token/时间预算、结果行数和 SQL 超时。

`plan-first-text2sql-v3` checkpoint 不会复用旧 8 节点协议或 `plan-first-text2sql-v2` 的状态。协议、Build、Gate 实现、图、计划 contract 或任一版本 pin 变化时，同一内部任务 ID 都会 fail closed，不会把旧 Agent 状态拼接进新运行。这里的 v3 指运行协议；`SchemaLinkPack/v2`、`text2sql-policy-v2` 与 SQLite checkpoint envelope 各有独立 contract version，不应随运行协议名称一起改写。Web 与 CLI 的 stable/candidate 都使用 `外部任务 ID + lane + Policy 版本` 生成独立键，避免 shadow/canary 双跑或 Policy 切换互相读取状态。

Store 使用 WAL、短连接和 `BEGIN IMMEDIATE`；同一任务只允许一个有效租约持有者执行。另一个线程或进程同时请求相同任务会得到 busy 错误，失败或租约过期后才可接管。

## 评测恢复

`run_text2sql_evaluation.py` 当前使用独立的 Case 级 append-only JSONL checkpoint。`evaluation_run_id` 写入 JSONL Header：只有 `--resume` 会复用它，新建评测即使数据集与 Policy 相同也会获得独立命名空间，不会把旧模型输出当作新 Benchmark；已完成 Case 由日志直接跳过。

当前评测脚本没有向 Engine 注入 SQLite Runtime Checkpoint Store，也没有为单题传入 Runtime `task_id`，因此只能恢复到 Case 边界，不能恢复某道题内部的 11 个节点。若后续接通单题节点恢复，任务键还必须包含 `evaluation_run_id + case_id`，并继续接受上述协议、图、contract 与 pins 的完整身份校验。

## 数据与运维边界

节点状态可能包含 DDL、检索证据、SQL 候选和查询结果，不能把该数据库当作只含哈希的 Shadow Store。生产环境应将文件放在受限目录、限制备份访问并设置符合数据策略的保留周期。`inspect(task_id)` 只返回状态、身份哈希、错误摘要、时间和节点数量，不返回节点正文或结果。
