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

## 2. 三类记忆

- 会话记忆：保存用户与会话维度的 QueryRun、独立问题、路由类型、结构化 QuerySpec / SchemaPlan、SQL、最终 Gate、结果摘要和最多 50 行结果快照，用于追问和结果问答。
- 知识记忆：KnowledgeStore 是权威层，保存 Schema、Wiki、关系、取值和审核后的 Question-SQL，继续执行状态、ACL、快照和版本校验。
- Skill 记忆：五个 Agent 的策略、失败归因与 stable/candidate Memory 仍由原 EvoAgent 自进化模块管理；Query Planning 与 SQL Generation 使用独立策略槽，不与 Vanna 混合。Harness 没有 Skill Memory。

使用阿里云模型时，Leader 路由会收到最近 QueryRun 的问题、SQL 与结果元数据；`RESULT_QA` 还会收到所引用 QueryRun 的列名及最多 50 行有限结果快照。完整 SQLite 文件不会上传，历史快照也不会提供给其他用户或会话。

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

1. 每次成功的独立查询记录为 QueryRun，并生成隔离候选经验。
2. 用户在问答页标记“正确”，或提交经过确定性 SQL 门禁的修正 SQL。
3. 管理员在“评测与人工审核”页批准或拒绝候选。
4. 批准后写入 KnowledgeStore 的 `verified_example` 稳定条目；不会在线直接修改正在使用的向量索引。
5. 离线运行 `build_text2sql_vanna.py`，新 Question-SQL 才进入下一个固定 Vanna 索引。
6. Policy 候选仍走 240 题 Validation/Sealed Holdout、Shadow、Canary 和人工激活流程；Memory 候选也必须经人审、240 题评测和显式激活后才可进入 stable。经验索引、Policy 发布和 Memory 发布是彼此隔离的演进线。

## 6. 可观测性

前端“运行轨迹”显示 Query Router 类型、父 QueryRun、独立问题、SchemaPlan、QuerySpec、Bound/ApprovedQueryPlan、Vanna 检索批次、五 Agent 轨迹、版本固定和 Harness 结果。“数据与知识”显示 Vanna 是否可用、条目数量以及生成/执行能力保持关闭。
