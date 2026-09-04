# EvoSQL Phase 2：Multi-Agent 基线

## 运行拓扑

当前实现直接复用 EvoAgent 的 `BoundedRole`、`ToolRegistry`、`AgentRuntime`、checkpoint 协议、ContextManager 和 ExecutionLedger，不是另建串行 Text2SQL Pipeline。

```text
Text2SQL Lead（委派）
  ├─ Schema & Grounding Worker（只查 Schema / 值 / stable 知识）
  └─ SQL Strategy Worker（独立 QuerySpec / SQL / AST / EXPLAIN）
Text2SQL Lead（检查，可定向返工一次）
SQL Critic（匿名候选盲审，不看 Worker 身份）
Text2SQL Lead（只能选择已有候选）
AST Safety → SchemaPlan 一致性 → EXPLAIN → 只读执行
```

Worker 彼此不通信。Schema Worker 不可生成或执行 SQL；SQL Strategy Worker 不可执行 SQL；Critic 不可生成新候选或执行 SQL。只有最终确定性节点可以通过 Lead 工具执行，且数据库仍以 `mode=ro&immutable=1 + PRAGMA query_only=ON` 打开。

## 首次运行

先完成本地数据和知识构建：

```bash
python scripts/build_text2sql_sqlite.py
python scripts/build_text2sql_knowledge.py
```

在 `.env` 中配置原 EvoAgent 已支持的 OpenAI-compatible 模型，然后执行：

```bash
python scripts/run_text2sql.py "强烈岩爆案例有多少个"
```

输出包含最终 SQL、结果、五项版本固定值、Gate、协作轨迹和模型/工具调用统计。没有配置模型时不会降级成单 Agent 或规则生成器，而是直接拒绝启动。

传入稳定的 `--task-id` 后，8 个 Runtime Node 会独立持久化；同一请求在进程中断后可从最后一个完成节点继续。具体恢复身份、并发租约和数据边界见 `CHECKPOINT_RUNBOOK.md`。

## 硬门禁

- 只接受单条 SQLite Query AST；
- 拒绝 INSERT、UPDATE、DELETE、DDL、控制语句、注释和多语句；
- 拒绝当前 Schema Snapshot 外的表列；
- 拒绝 `load_extension`、`readfile`、`writefile`；
- CTE 和子查询可以使用，但底层物理表仍须在白名单；
- SQL 使用的物理表列必须包含在 Schema Worker 的 SchemaPlan；
- 跨表 Join 必须引用已进入 `stable` 的 relationship evidence；
- SQL Strategy 的候选必须原样通过 `validate_sql` 和 `explain_sql`；
- 最终执行带 wall-clock 中断和返回行数上限；
- Critic 拒绝的候选必须由 Lead 显式记录 objection resolution，否则拒绝执行。

模型无法绕过这些检查。即使 Lead、两个 Worker 和 Critic 都输出同意，写 SQL 仍不会进入 SQLite。

## 当前范围

这是 Phase 2 当时的固定 Policy 基线。Phase 4 已在独立 validation/holdout 之上启用受控候选、人工审核 Memory、晋升闸门与回滚；边界和操作方式见 `PHASE4_SELF_EVOLUTION.md`。

## 验证

```bash
python -m unittest \
  tests.test_text2sql_phase0 \
  tests.test_text2sql_knowledge \
  tests.test_text2sql_phase2 -v
```

Phase 2 测试使用脚本化模型走完整角色决策和真实只读 SQLite 执行，验证正确查询返回 `6`，并验证写候选在模型链路后仍被确定性拒绝。
