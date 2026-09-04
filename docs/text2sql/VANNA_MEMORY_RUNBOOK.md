# Text2SQL 会话记忆与 Vanna 检索

## 1. 运行拓扑

```text
用户问题
   |
   v
Text2SQL Lead（Query Router + 会话上下文）
   |-- RESULT_QA ------> 认证 QueryRun 快照，Harness 只做确定性结果 replay
   |
   `-- DATA_QUERY / FOLLOW_UP_QUERY
           Evidence Orchestration：stable KnowledgeStore + 可选 Vanna retrieval-only
                        |
               +--------+--------+
               |                 |
      Schema Grounding      Query Planning
      物理 Schema/值/关系    schema-blind，仅业务术语
               +--------+--------+
                        |
        deterministic bind → Lead 语义审核/有界返工
                        |
          Harness 铸造 ApprovedQueryPlan
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

这是 `plan-first-text2sql-v3` 实现 Multi-Agent 语义的固定、有限 11 节点运行图。五个 Agent Role 是 `text2sql-lead`、`schema-grounding`、`query-planning`、`sql-generation`、`text2sql-critic`；并发只发生在 plan-workers 节点内的 Schema Grounding 与 Query Planning 之间。Lead 负责路由、委派、计划语义审核、有界返工和最终候选选择。Harness 负责绑定、批准计划铸造、候选门禁和只读执行，是不可绕过的确定性边界，不是 Agent，也不是 Skill。当前不会在 Critic reject 后再次修复 SQL。

## 2. 四层记忆

- Working Memory：保存当前 user / session 最近 100 条用户与助手消息，用于本轮上下文。
- Episodic Memory：每个 user / session 保存最近 50 个 QueryRun；每个 QueryRun 包含独立问题、路由类型、结构化 QuerySpec / SchemaPlan、SQL、最终 Gate、结果摘要和最多 50 行结果快照，用于追问、结果问答与审计。
- Question-SQL Memory：用户确认正确的 Question-SQL 以 `verified_example` 写入 stable KnowledgeStore，再构建版本化 Vanna Stable 索引；经验账本同步标记为 `promoted`，并用 `knowledge_evidence_id` 关联两侧记录。
- Agent Semantic Memory：五个 Agent 的失败归因经验按 stable / candidate 分层治理；Query Planning 与 SQL Generation 使用独立策略槽，不与 Vanna 混合。Harness 没有 Skill Memory。

使用阿里云模型时，Leader 路由会收到最近 QueryRun 的问题、SQL 与结果元数据；`RESULT_QA` 还会收到所引用 QueryRun 的列名及最多 50 行有限结果快照。完整 SQLite 文件不会上传，历史快照也不会提供给其他用户或会话。

记忆中心默认读取当前浏览器会话；如果当前会话为空但同一用户存在历史记录，页面会明确标注“历史会话回看”并展示最近一个会话。该回看只影响可视化，不会把历史 Working Memory 合并进新会话的 Agent 上下文。

FOLLOW_UP_QUERY 必须显式选择一个当前会话内的 parent；系统不会在 Lead 漏填时静默绑定最近任务。父快照还必须通过 task、user/session、success、最终 Gate、完整版本 pins、严格 QuerySpec/SchemaPlan 与绑定指纹校验，否则整个追问 fail closed，Lead 的 standalone rewrite 也不会继续驱动 Worker。独立问题改写不是事实来源：新 filter 值只能来自本轮原始问题，或来自认证父 QuerySpec；显式 Join 只能来自原始问题的确定性等式解析或可信父 SchemaPlan。该结构化 provenance 只供 Harness 校验，不会把父物理 Schema 暴露给 Query Planning。RESULT_QA 同样要求认证父快照，并且仅允许明确的结果重显；文案从缓存列/行确定性生成，任何比较、过滤、排序或计算要求都会转为新查询。

## 3. Vanna 的权限边界

`VannaRetrieverOnly` 只暴露三类检索：DDL、Documentation、Question-SQL。包装层显式封锁 `ask`、`generate_sql`、`submit_prompt` 和 `run_sql`；数据库连接也不会交给 Vanna。Vanna 不起草候选 SQL，也不会绕过计划协议直接向 SQL Generation 喂入 SQL；它只在 Evidence Orchestration 中增强 stable 证据召回。

Vanna 命中不能直接成为事实。每个向量条目携带 KnowledgeStore 的 `evidence_id`，返回后必须重新检查：

1. 条目仍为 `stable`；
2. 数据库快照仍一致；
3. 当前 Principal 仍满足 ACL；
4. 当前消费阶段允许使用该知识类型。

任何检查失败都丢弃命中。Query Planning 还会再次过滤检索结果，只保留已审核的 `business_glossary`，不接收 DDL、物理列、实库值、关系或 Question-SQL 示例；Schema Grounding 可使用授权的物理证据。SQL Generation 只接收不可变 `ApprovedQueryPlan`，Blind Critic 只接收通过 Harness 门禁的匿名候选。索引缺失、依赖不可用或版本不匹配时，系统自动回退到 KnowledgeStore 原检索。

## 4. 构建稳定索引

```bash
python scripts/build_text2sql_knowledge.py
python scripts/build_text2sql_vanna.py
```

索引目录为 `artifacts/text2sql/vanna/<stable-index-version>/`。构建脚本只读取 KnowledgeStore 中已经审核为稳定的条目，并写入固定版本目录。

可通过以下环境变量控制：

```env
EVOAGENT_TEXT2SQL_VANNA_ENABLED=true
EVOAGENT_TEXT2SQL_VANNA_ROOT=artifacts/text2sql/vanna
```

## 5. Question-SQL 经验闭环

1. 每次成功的独立查询记录为 QueryRun；在收到用户反馈前，Question-SQL 经验处于 `ineligible / requires_human_feedback`。
2. 用户点击“确认结果正确”后，系统重新检查 QueryRun 来源、最终 Harness Gate 和当前 Schema Snapshot，并再次执行确定性 SQL Gate。
3. 校验通过后，Question-SQL 以 `verified_example` 直接写入 stable KnowledgeStore，并同步构建新的不可变 Vanna Stable 版本。
4. 经验账本把该记录标记为 `promoted`；后续查询可以立即从 Vanna 召回，但命中仍必须回到 KnowledgeStore 复验 stable、snapshot 与 ACL。
5. 用户点击“结果不正确”时，原经验变为 `rejected`，系统执行确定性错误归因并生成单一 Agent Role 的 Semantic Memory Candidate；可选修正 SQL 作为独立 Question-SQL 候选保存，避免覆盖原始证据。
6. 错误归因产生的 Agent Memory 与 Policy 候选仍走人工审核、240 题 Validation/Sealed Holdout、Shadow、Canary 和显式激活。正反馈 Question-SQL 只增强检索，不修改 Agent Policy，也不绕过 Binder、Critic 或最终 SQL Gate。

## 6. 可观测性

前端“运行轨迹”显示 Query Router 类型、父 QueryRun、独立问题、SchemaPlan、QuerySpec、Bound/ApprovedQueryPlan、Vanna 检索批次、五 Agent 轨迹、版本固定和 Harness 结果。“数据与知识”显示 Vanna 是否可用、条目数量以及生成/执行能力保持关闭。
