# Text2SQL Vanna 与 Memory 运行手册

> 当前 Memory 治理以 [Memory / Policy MVP 运行手册](MEMORY_EVOLUTION_MVP_RUNBOOK.md) 为准。本文保留 Vanna、会话 Memory 和 Legacy `AgentSemanticRule/v1` 的背景说明；Legacy Rule 仅兼容读取，不再是新自进化主链。

## 1. 一眼看清边界

```text
Schema Snapshot ────────→ Vanna DDL / Schema Documentation ─┐
Business Markdown ──────→ Vanna Documentation ──────────────┼─→ RAG 召回
Confirmed Question-SQL ─→ Vanna Question-SQL ───────────────┘

Join Catalog ───────────→ Schema Grounding / Binder
Working Memory ─────────→ 当前会话上下文
Episodic Memory ────────→ QueryRun 历史与追问
Semantic Experience ────→ 离线 Policy Candidate 的有审计来源
```

当前是单用户模式。业务 Markdown 默认可信并直接构建 Vanna，不设置独立 KnowledgeBase、业务知识审批流或 ACL。Join Catalog 和 Memory 各自独立，不能因为某段内容被 Vanna 召回就改变其确定性边界。

## 2. 运行拓扑

```text
用户问题
   |
   v
Text2SQL Lead（Query Router + 会话上下文）
   |-- RESULT_QA ------> 认证 QueryRun 快照，Harness 确定性 replay
   |
   `-- DATA_QUERY / FOLLOW_UP_QUERY
           Evidence Orchestration：Vanna + Schema Snapshot + Join Catalog
                        |
               +--------+--------+
               |                 |
      GroundingPack/v1    PlanningBusinessPack/v1
      物理 Schema/值/关系    schema-blind 业务语义
               |                 |
      Schema Grounding      Query Planning
               +--------+--------+
                        |
        deterministic bind → Lead 语义审核/有界返工
                        |
          Harness 铸造 ApprovedQueryPlan
                        |
        Vanna Question-SQL → SQL/plan scope 复验
                        |
             VerifiedExamplePack/v1（≤3）
                        |
                 SQL Generation
                        |
      Harness：validate / plan conformance / EXPLAIN
       （零候选通过时仅一次 Generation repair）
                        |
          Blind Critic → Lead 最终选择
                        |
       Harness：最终门禁 / SQLite 只读执行
```

这是 `plan-first-text2sql-v3` 的 11 节点运行图。并发只发生在 Schema Grounding 与 Query Planning 两个 Plan Worker 之间。Vanna backend 仍只检索、不执行 SQL；第 2 节点另有一个无工具、无连接的请求级组件，使用冻结的 Vanna 上下文生成仅供 Schema 反向提取的草稿 SQL。草稿不能绕过 Binder、Critic 或最终 Gate。

## 3. 三种 Memory 与一种 RAG 案例

| 类型 | 保存内容 | 写入时机 | 用途 |
|---|---|---|---|
| Working Memory | 最近消息：`role + content + task_id + session_id + created_at` | 每轮自动写入，滚动保留 | Lead 理解当前会话 |
| Episodic Memory | 完整 QueryRun：问题、Plan、SQL、Gate、结果、反馈、时间、轮次和版本 | 每次查询结束写入 | 追问、结果回看、根因分析与审计 |
| Semantic Experience | `ExperienceMemory/v1`：来源任务、owner Agent、问题、修正、适用条件和不可变证据 | 用户纠错或确定性修订后形成 candidate / needs_evidence | 不直接注入 Agent；Confirmed 只能编译为 Policy Candidate |
| Vanna Question-SQL | 用户问题与确认正确、复验通过的 SQL | 用户确认正确后直接写入 | ApprovedQueryPlan 后召回相似结构 |

Question-SQL 是 Vanna RAG 案例，不是 Memory。正确反馈不会修改 Agent Policy；错误反馈才可能沉淀为 Semantic Experience Candidate。

新 Experience 始终 `runtime_eligible=false`，不会被 Vanna 或 Runtime Prompt 直接检索。默认生产治理链为：`candidate / needs_evidence → 人工 confirm → prompt_fragment-only Policy Candidate → Target Replay → 96 条独立评测 → Shadow → Canary → 人工激活`。受限自动 MVP 只接纳机器可验证 Experience，经离线 SemanticRule 归纳与同 Agent Prompt 编译后，必须通过 Target Replay、96 条评测及身份/来源复核才可原子激活。

## 4. Vanna 的三种数据

### DDL / Schema Documentation

来自当前 Schema Snapshot，向 Schema Grounding 提供表、列、类型、注释和值域线索。最终物理绑定仍须回到 Snapshot 校验，不能仅凭向量结果使用表列。

### Business Documentation

来自 `knowledge/business`，向 Query Planning 提供实体、维度、指标、粒度、过滤、去重、NULL 和值语义。业务 Markdown 在单用户模式下默认可信，修改后直接重建，不经过 Candidate / Stable。

### Question-SQL

只保存用户确认正确且重新通过 SQL Gate 的样例。它只在 ApprovedQueryPlan 形成后召回；候选 SQL 必须满足当前计划的表列范围和安全规则，最多 3 条进入 `VerifiedExamplePack/v1`。

## 5. Vanna 的安全边界

`VannaRetrieverOnly` 只暴露 DDL、Documentation 和 Question-SQL 检索。包装层封锁 `ask`、`generate_sql`、`submit_prompt` 和 `run_sql`，数据库连接不会交给 Vanna。

单用户模式不做 Principal ACL 或 KnowledgeStore 状态回源，但保留以下确定性检查：

1. Schema 证据必须与当前 Snapshot 一致；
2. Query Planning 只能看到业务语义，不能看到 DDL、Join 或 SQL 示例；
3. Join 必须由独立 Join Catalog 或用户问题中的确定性等式提供；
4. Question-SQL 必须再次通过只读 AST Gate；
5. Question-SQL 的表列、Join、谓词值和结果粒度不能超出 ApprovedQueryPlan；
6. 最终 SQL 仍须通过 validate、plan conformance、EXPLAIN 与执行前复验。

## 6. 构建索引

```bash
python scripts/build_text2sql_vanna.py
```

索引位于 `artifacts/text2sql/vanna/`，corpus version 由 Schema Snapshot、业务文档摘要和 Question-SQL 集合共同决定。业务文档摘要不是独立运行时 Pin；wire 兼容字段 `wiki_index_version` 当前保存同一个 Vanna corpus version，`vanna_index_version` 出现时也只是该版本的镜像。

```env
EVOAGENT_TEXT2SQL_VANNA_ENABLED=true
EVOAGENT_TEXT2SQL_VANNA_ROOT=artifacts/text2sql/vanna
```

当前项目只保留 VannaCorpus 检索链路。业务 Markdown 使用 `evoagent/text2sql/business_documents.py` 解析；重建命令为 `python scripts/build_text2sql_vanna.py`。

## 7. Question-SQL 闭环

1. 查询完成后形成 Episodic QueryRun；尚未收到反馈时，不写入 Vanna Question-SQL。
2. 用户点击“确认结果正确”，系统检查 QueryRun 来源、最终 Gate 和当前 Schema Snapshot，并重新运行 SQL Gate。
3. 复验通过后，Question-SQL 直接写入 Vanna；QueryRun 保存其来源关联。
4. 后续查询只在 ApprovedQueryPlan 形成后召回它，并执行当前计划范围复验。
5. 用户点击“结果不正确”，该 SQL 不进入或从 Question-SQL 集合移除；有充分证据时，系统按错误类型生成单一 Agent Role 的 Semantic Experience Candidate。
6. Experience 只能先生成同一 Agent 的 `prompt_fragment` Policy Candidate；默认生产链继续走 Shadow/Canary/人工发布，受限自动链必须走 Target Replay、96 条独立评测与完整身份复核，且两条链都支持回滚。

## 8. 会话规则

Working Memory 可以保存多条消息，但只属于当前 session；Episodic Memory 每次 QueryRun 形成一条记录，一个会话可以有多条。历史会话回看只影响可视化，不会自动并入新会话上下文。

FOLLOW_UP_QUERY 必须引用当前会话内通过最终 Gate 的父 QueryRun。父快照的计划、结果与版本不完整时 fail closed，不会静默绑定最近任务。RESULT_QA 只允许重显已认证结果；比较、过滤、排序或重新计算会进入新的 DATA_QUERY。

## 9. 可观测性

Execution Ledger 记录三类检索批次、`VerifiedExamplePack/v1` 的接收数量与拒绝原因。前端展示 Vanna 的 DDL / Documentation / Question-SQL 数量，以及 Working、Episodic、Semantic Experience；Legacy Runtime Memory 单独标为兼容区。
