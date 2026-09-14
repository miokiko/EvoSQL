# Text2SQL 业务 Markdown 契约

> `WIKI_CONTRACT.md` 是历史文件名。当前项目没有网络 Wiki，也没有独立 KnowledgeBase；业务知识源是版本库中的 `knowledge/business`。

## 单用户约定

- `knowledge/business/**/*.md`（模板除外）默认可信并直接构建为 Vanna Documentation。
- 不设置业务知识 Candidate / Stable、审核人或多用户 ACL。
- Vanna 是检索索引，不是内容编辑源；修改知识应编辑 Markdown 后重建索引。
- 业务文档不复用 Experience 或 Policy 的审核/发布状态；三者是独立生命周期。

## 最小页面内容

每个页面必须有稳定的 `page_id`、标题、业务类型和适用的 Schema Snapshot。指标页面还应写明指标名称、别名、定义、统计粒度、聚合方式、过滤条件、NULL 规则和涉及字段；关系说明应写明左右字段、基数、目标粒度、去重规则和 fanout 风险。

历史页面中的 `owner_id`、`allowed_principals` 和 `knowledge_type` 可以保留用于格式兼容，但单用户默认链路不使用这些字段做审批或授权。

## 权威与冲突

1. Schema Snapshot 负责表、列、类型、索引和观测值等物理事实。
2. 业务 Markdown 负责实体、术语、指标、维度、粒度、值语义和统计口径。
3. Join Catalog 负责可执行的连接键、基数与 fanout 约束；业务文档提到某个 Join 不等于授权使用。
4. Vanna 负责检索 DDL、Documentation 与已确认 Question-SQL，不生成或执行 SQL。
5. Memory 记录会话、QueryRun 和 Semantic Experience，不能覆盖 Schema 或业务文档；Experience 不直接进入运行时。
6. Markdown 中的表列名无法绑定当前 Schema 时，构建直接失败；互相冲突的业务定义也必须先在源文件解决。

运行与回放固定四个逻辑版本：

```text
database_snapshot_id
vanna_corpus_version
memory_snapshot_id
policy_version
```

业务文档摘要已经参与 `vanna_corpus_version` 的内容指纹计算，不是第五个独立 Pin。为兼容旧 checkpoint，wire 仍使用 `wiki_index_version` 字段承载同一个 Vanna corpus version；`vanna_index_version` 出现时也镜像该值，不能把二者理解为 Wiki 与 Vanna 两套版本。

## RAG 消费边界

计划前，Vanna 分别辅助召回 Schema Grounding 所需的物理说明，以及 Query Planning 所需的 schema-blind 业务语义。ApprovedQueryPlan 形成后，Harness 才召回 Question-SQL，并重新执行 SQL 安全和当前计划表列范围检查；通过者最多 3 条，只作为 SQL Generation 的结构参考。

Vanna 索引命中不能授权越界表列或 Join。Schema 仍以当前 Snapshot 为准，Join 仍以独立 Join Catalog 为准，最终执行仍必须通过 Harness 的确定性 Gate。

## 历史兼容

当前项目只保留 VannaCorpus 检索链路。业务 Markdown 使用 `evoagent/text2sql/business_documents.py` 解析；重建命令为 `python scripts/build_text2sql_vanna.py`。
