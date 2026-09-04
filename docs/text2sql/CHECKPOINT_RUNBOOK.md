# Text2SQL 节点级 Checkpoint 运行手册

## 已接通的恢复链路

`Text2SQLAgenticEngine` 的 8 个运行节点在每个节点成功后立即提交到独立 SQLite Store：

1. Lead 路由与委派；
2. Evidence / DraftLinkPack 编排；
3. Schema Grounding 与 SQL Strategy 并行 Worker；
4. Lead 评估；
5. 定向返工；
6. blind Critic；
7. Lead 最终选择；
8. AST / SchemaPlan / EXPLAIN 门禁与只读执行。

失败会保留已完成节点和累计 `ExecutionLedger`；普通运行异常还会记录失败节点的 attempt/error。进程重启后，相同任务从首个未完成节点继续；最终结果也会持久化，因此完成后的重复请求不会再次调用模型或执行 SQL。

默认存储位置：

```bash
EVOAGENT_TEXT2SQL_CHECKPOINT_STORE=artifacts/text2sql/checkpoints/runtime.sqlite3
```

CLI 恢复时必须复用原任务 ID：

```bash
python scripts/run_text2sql.py "强烈岩爆案例有多少个" --task-id request-0001
```

不传 `--task-id` 会生成新 ID，并在结果的 `task_id` 字段中返回；该次运行仍会持久化，但下一次只有显式复用这个 ID 才能恢复。

Web 页面会在浏览器本地暂存未完成请求的 `task_id`；网络或服务中断后再次发送同一问题会复用该 ID，成功返回后清除。API 调用方也应在首次请求前生成并重试复用自己的 `task_id`。

Web 服务还在 Evolution Store 中保存一层请求记录：首次请求会冻结该次会话上下文与 Runtime 身份，完成后缓存对外响应；相同 `task_id` 的重试会复用冻结上下文，且 QueryTrace、会话消息和经验候选均按任务幂等写入，避免进程在“节点完成、响应未返回”之间退出时产生重复副作用。

## 身份与隔离

Checkpoint 身份绑定以下输入：

- 问题与会话 QueryRun 上下文的 SHA-256；
- principals（角色级 Tool ACL 的调用身份）；
- database snapshot、Wiki、Vanna、Memory、Policy 五项版本固定值；
- 模型 provider/model 与 temperature；
- 图协议、节点顺序、Token/时间预算、结果行数和 SQL 超时。

同一内部任务 ID 的任一绑定项发生变化都会 fail closed，不会把旧 Agent 状态拼接进新运行。Web/CLI 的 stable 与 candidate 使用 `外部任务 ID + lane + Policy 版本` 生成独立键，避免 shadow/canary 双跑互相读取状态。

Store 使用 WAL、短连接和 `BEGIN IMMEDIATE`；同一任务只允许一个有效租约持有者执行。另一个线程或进程同时请求相同任务会得到 busy 错误，失败或租约过期后才可接管。

## 评测恢复

`run_text2sql_evaluation.py` 保留原有 Case 级 append-only JSONL，同时给每个 Case 分配稳定的 Runtime 任务键：

```text
text2sql-eval:<evaluation_run_id>:<case_id>
```

`evaluation_run_id` 写入 JSONL Header：只有 `--resume` 会复用它，新建评测即使数据集与 Policy 相同也会获得独立命名空间，不会把旧模型输出当作新 Benchmark。Case 级日志负责跳过已完成题目，SQLite Runtime Store 负责恢复单题内部节点。可用 `--runtime-checkpoint-store` 单独指定路径。

## 数据与运维边界

节点状态可能包含 DDL、检索证据、SQL 候选和查询结果，不能把该数据库当作只含哈希的 Shadow Store。生产环境应将文件放在受限目录、限制备份访问并设置符合数据策略的保留周期。`inspect(task_id)` 只返回状态、身份哈希、错误摘要、时间和节点数量，不返回节点正文或结果。
