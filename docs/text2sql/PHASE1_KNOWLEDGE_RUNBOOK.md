# Text2SQL Phase 1：单用户 RAG 运行手册

## 当前边界

本项目采用单用户知识链，不再维护独立 KnowledgeBase。Vanna / Chroma 直接索引三类输入：

| 输入 | Vanna 类型 | 权威来源 |
|---|---|---|
| 当前 Schema Snapshot | DDL / Documentation | `artifacts/text2sql/schema/database_snapshot.json` |
| 业务 Markdown | Documentation | `knowledge/business/**/*.md` |
| 用户确认且通过 SQL Gate 的 Question-SQL | Question-SQL | 本地确认记录与 QueryRun 来源 |

业务 Markdown 默认可信，不走 Candidate → Stable 审批，也不做多用户 ACL。新的 Agent 行为经验使用 `ExperienceMemory/v1` 的 candidate / confirmed 生命周期；部署行为由独立 Policy 发布链治理。

Join Catalog 与 Vanna 分开：`join_catalog.review.json` 保存可供确定性绑定使用的关系、基数和 fanout 信息；向量检索命中某段关系文字本身不能授权 Join。

## 构建 RAG 索引

先生成 Schema Snapshot 和只读 SQLite，再构建 Vanna：

```bash
python scripts/generate_text2sql_schema.py
python scripts/build_text2sql_sqlite.py --replace
python scripts/build_text2sql_vanna.py
```

默认输入：

- `artifacts/text2sql/schema/database_snapshot.json`
- `knowledge/business/**/*.md`
- 本地已经确认并复验通过的 Question-SQL

默认输出位于 `artifacts/text2sql/vanna/`。索引版本由 Schema、业务文档和 Question-SQL 内容共同确定；相同输入重复构建不应产生重复条目。

当前项目只保留 VannaCorpus 检索链路。业务 Markdown 使用 `evoagent/text2sql/business_documents.py` 解析；重建命令为 `python scripts/build_text2sql_vanna.py`。

## 运行时角色视图

Evidence Orchestration 按消费阶段过滤同一个 Vanna 索引：

```text
Schema Snapshot + Vanna Schema Documentation + Join Catalog
  → GroundingPack/v1 + SchemaLinkPack/v3
  → Schema Grounding

Vanna Business Documentation
  → PlanningBusinessPack/v1
  → schema-blind Query Planning

ApprovedQueryPlan + Vanna Question-SQL
  → SQL 安全与 plan scope 复验
  → VerifiedExamplePack/v1（最多 3 条）
  → SQL Generation
```

Query Planning 不接收 DDL、物理标识符、实库值、Join 或 Question-SQL。Schema Grounding 不接收 Question-SQL。SQL Generation 只能以 ApprovedQueryPlan 为语义约束；样例只提供结构参考，不能扩大计划中的表列、Join、过滤值或结果粒度。

Vanna 不生成或执行 SQL。包装层封锁 `ask`、`generate_sql`、`submit_prompt` 和 `run_sql`，数据库连接只由 Harness 持有。

## 业务 Markdown 约定

新页面从 `knowledge/business/TEMPLATE.md.example` 创建。一个页面只描述一类业务概念，并遵守以下约定：

- `page_id` 稳定且唯一；
- 表列引用使用全限定名；
- 指标写清业务定义、统计粒度、聚合、去重、NULL 与默认过滤；
- 不放完整 Question-SQL；正确 SQL 走 Question-SQL 通道；
- 不写 Agent Prompt、工具调用或行为经验；这些分别属于 Semantic Experience 与 Policy；
- 内容与当前 Schema 无法绑定时构建失败，直接修正文档或 Schema，不创建待审候选。

单用户模式不需要负责人审批或 ACL。历史 frontmatter 中的 `owner_id`、`allowed_principals` 可作为兼容信息保留，但不参与默认运行授权。

## 更新与删除

- 修改 Schema：重新生成 Snapshot，并重建 Vanna；旧索引不能与新快照混用。
- 修改业务文档：重建 Vanna；内容摘要变化会产生新的索引版本。
- 删除业务文档：重建后移除对应 Documentation。
- 用户确认 SQL 正确：再次运行 SQL Gate，通过后写入 Question-SQL 集合。
- 用户撤销确认或发现 SQL 错误：从 Question-SQL 集合移除，并保留 QueryRun 追溯信息。
- 修改 Join：更新独立 Join Catalog；不通过编辑 Vanna 文档绕过 Binder。

## 兼容说明

当前项目只保留 VannaCorpus 检索链路。业务 Markdown 使用 `evoagent/text2sql/business_documents.py` 解析；重建命令为 `python scripts/build_text2sql_vanna.py`。
