# Text2SQL Phase 2：EvoAgent Multi-Agent 基线

## 运行拓扑

当前 `plan-first-text2sql-v3` 实现直接复用 EvoAgent 的 `BoundedRole`、`ToolRegistry`、`AgentRuntime`、checkpoint 协议、ContextManager 和 ExecutionLedger，以 11 个固定 Runtime Node 承载 Multi-Agent 协作，不是单 Agent 的一次性 Text2SQL 调用。

```text
Text2SQL Lead（路由、委派）
  └─ Evidence Orchestration（stable 检索 + SchemaLinkPack/v2）
       ├─ Schema Grounding ──→ SchemaPlan（物理表列、Join、值绑定、粒度）
       └─ Query Planning   ──→ QuerySpec（逻辑意图、聚合、过滤、排序、形状）
                    │             上述两个 Plan Worker 仅在此处并发
                    ▼
Deterministic Bind ──→ BoundQueryPlan
Text2SQL Lead 语义审核 ──→ 必要时每个 Plan Worker 最多定向返工一次
Harness 铸造不可变 ApprovedQueryPlan
SQL Generation ──→ 最多 4 个只读 SQLCandidate
Harness：validate_sql → plan conformance → explain_sql
  └─ 若零候选通过，只允许 SQL Generation 修复一次并重新经过全部门禁
Blind Critic（匿名候选盲审）
Text2SQL Lead（只能选择 Critic 接受的已有候选）
Harness：最终 validate_sql + plan conformance → 只读 execute_sql
```

五个 Agent Role 是 `text2sql-lead`、`schema-grounding`、`query-planning`、`sql-generation` 和 `text2sql-critic`。Harness 是应用持有的确定性执行主体，不是 Agent，也不是可演化 Skill；只有 Harness 拥有 `execute_sql`。

Worker 彼此不通信。Query Planning 对物理 Schema 保持 blind：只看到问题和经过审核的业务术语，不接收 DDL、物理列、实库值、关系或 SQL 示例。Schema Grounding 与 Query Planning 之外的节点均按固定顺序串行执行。数据库始终以 `mode=ro&immutable=1 + PRAGMA query_only=ON` 打开。

FOLLOW_UP_QUERY 必须由 Lead 明确引用会话内存在的父 QueryRun；未给出、越权、失败、版本漂移或计划指纹不一致的 parent 会直接 fail closed，不会自动回退到“最近一轮”，也不能继续使用 Lead 的 standalone rewrite 生成 SQL。服务端按 user/session 重新读取父结果，只有 `status=success` 且最终 Harness Gate 已接受的结构化 QuerySpec / SchemaPlan 才能提供继承值或显式 Join。Lead 写出的 standalone question 只帮助 Worker 理解追问，不会扩大任何事实来源；其中新增的物理 Schema 或 SQL 会在进入 Planning 前被撤回。

RESULT_QA 不是自由文本生成旁路。当前只支持明确要求重显上一轮结果的 replay；父快照必须通过同一套 scope、pins、plan 与 fingerprint 认证，最终 `summary_text` 由 Harness 从缓存列/行确定性渲染，Lead 的 `answer_text` 不进入 accepted 输出。涉及比较、筛选、排序、聚合或其他计算时返回 `needs_new_query`。

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

输出包含最终 SQL、结果、database/Wiki/Vanna/Memory/Policy 五项版本固定值、Gate、协作轨迹和模型/工具调用统计。没有配置模型时不会降级成单 Agent 或规则生成器，而是直接拒绝启动。Web 服务或直接调用 Engine 时若注入 checkpoint store，并传入稳定 `task_id`，11 个固定 Runtime Node 会逐节点持久化；CLI 与评测脚本的当前恢复边界见 `CHECKPOINT_RUNBOOK.md`。

## 硬门禁

- 只接受单条 SQLite Query AST；
- 拒绝 INSERT、UPDATE、DELETE、DDL、控制语句、注释和多语句；
- 拒绝当前 Schema Snapshot 外的表列；
- 拒绝 `load_extension`、`readfile`、`writefile`；
- SQL Safety 会解析 CTE/子查询并检查底层表；当前 QueryPlan/v1 的最终一致性门仅放行严格的 EXISTS 标量形状，其余 CTE、子查询和集合运算保守拒绝；
- Query Planning 只能生成逻辑 QuerySpec，不能选择物理表列或提前生成 SQL；
- QuerySpec 与 SchemaPlan 必须由 model-free binder 完整、唯一地绑定为 BoundQueryPlan；缺失或歧义不会用模糊匹配猜测；
- SchemaPlan 的物理表列必须属于固定 Schema Snapshot；
- logical value 必须具有可信来源：保守匹配本轮原始用户问题中的表面形式，或继承自同一 user/session 下、最终 Gate 已接受的父 QueryRun 的类型化 filter；Lead 生成的 standalone rewrite 本身永远不是来源；
- 非同值的 logical-to-physical 映射必须来自 `schema-grounding` 独占的已审核 value alias；LIKE 仅额外允许 Harness 确定性添加首尾 `%`；
- `eq` / `in` 的 physical value 必须在固定只读数据库中真实存在；范围边界与 LIKE 模式不要求恰好命中某行，但必须与固定列类型兼容；
- SchemaBinding 与 stable Join 的 evidence id 必须属于本轮 Grounding 已观察且对当前 Principal 可见的 stable/snapshot 证据；模型填写的任意 id 不构成授权；
- 推断出的跨表 Join 必须引用上述 relationship evidence；`user_explicit` Join 只能来自原始问题的确定性 qualified-column 等式解析，或已认证父 QueryRun 的结构化 SchemaPlan，不能由 Lead 改写或模型声明授权；
- 多表 SchemaPlan 的 Join 图必须覆盖且连通；QueryPlan/v1 只表示逐表单列等值 `INNER/LEFT JOIN`，CROSS、逗号连接、自连接、复合键和额外 `ON` 条件均保守拒绝；
- QueryPlan/v1 尚未携带可机器证明的 cardinality、唯一键与去重保持契约，因此未聚合的 `expected_shape=rows + JOIN` 一律拒绝，不能仅凭“投影了 result_grain”假定不存在 fanout；scalar、grouped_rows 与 existence 仍可在其各自计划约束下使用 Join；
- SQL Generation 只能消费由 Lead 做语义决策、Harness 铸造的不可变 ApprovedQueryPlan；
- 每个候选必须依次通过 `validate_sql`、ApprovedQueryPlan conformance 和 `explain_sql`；
- `OFFSET`、HAVING、未建模的行级 `DISTINCT`、非默认 NULL 排序、重复输出别名和计划外投影顺序均会被 conformance gate 拒绝；标量查询只能省略 LIMIT 或使用 `LIMIT 1`，Ranking 必须有明确排序槽；
- 首轮零候选通过时，SQL Generation 最多修复一次；修复发生在 Blind Critic 之前；
- Critic 输出必须覆盖每个候选且每个候选恰好一个决定；Critic 拒绝后当前实现不会再次生成或修复 SQL；
- Lead 最终只能选择 Critic 明确接受的已有候选；最终节点会再次校验 AST、计划 fingerprint、版本 pins 和 plan conformance；
- 最终执行带 wall-clock 中断和返回行数上限；
- 任一 Agent 都不能执行 SQL，只有最终 Harness 门禁通过后才能调用 `execute_sql`。

模型无法绕过这些检查。即使其余 Agent 输出同意，写 SQL、越界 Schema、未批准计划或被 Critic 拒绝的候选仍不会进入 SQLite。

## 当前范围

这是 Phase 2 当时的固定 Policy 基线。Phase 4 已在独立 validation/holdout 之上启用受控候选、人工审核 Memory、晋升闸门与回滚；边界和操作方式见 `PHASE4_SELF_EVOLUTION.md`。

## 验证

```bash
python -m unittest \
  tests.test_text2sql_phase0 \
  tests.test_text2sql_knowledge \
  tests.test_text2sql_phase2 -v
```

Phase 2 测试使用脚本化模型走完整 11 节点图和真实只读 SQLite 执行，验证正确查询返回 `6`，并验证写候选在模型链路后仍被确定性拒绝。LLM 调用数会随计划返工、零通过修复和 RESULT_QA 分支变化，不作为节点数。
