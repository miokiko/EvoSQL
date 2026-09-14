# Text2SQL 业务知识文档说明

## 定位

`knowledge/business` 是本项目人工维护的业务语义来源，负责回答“业务对象是什么、一个对象怎么算、业务词对应什么维度、缺失值如何解释”。项目当前是单用户模式：仓库中的业务 Markdown 保存后直接参与下一次 Vanna Documentation 构建，不经过独立 KnowledgeBase 或 PR 审批。文件可以收录，不代表其中候选业务解释已经确认；内容状态随每个片段一起返回。

它不复制数据库 DDL，不保存完整 Question-SQL，也不保存 Agent 的行为经验。

## 职责边界

| 内容 | 唯一归属 | 如何进入运行链路 |
|---|---|---|
| 表、列、类型、主键、字段注释和观测值 | Schema Snapshot | 构建为 Vanna DDL / Schema Documentation |
| 业务实体、术语、维度、指标、粒度、单位和解释限制 | `knowledge/business` | 直接构建为 Vanna Documentation |
| 连接键、基数、目标粒度与 fanout 风险 | Join Catalog | Schema Grounding 与 Binder 确定性读取，不以向量命中授权 Join |
| 用户确认且通过 SQL Gate 的 Question-SQL | Vanna Question-SQL | 确认后直接写入 Question-SQL 集合 |
| 当前对话和已完成 QueryRun | Working / Episodic Memory | 按 session 读取 |
| 某个 Agent 应如何避免同类错误 | Semantic Experience | 经人工确认或机器可验证证据准入后，只能编译成同 Agent Policy Candidate，再走 Target Replay 与发布门禁 |

Vanna 是 Schema、业务文档和已确认 Question-SQL 的统一 RAG 索引。它不是业务文档编辑源，也不是 Agent Memory。

## 默认信任规则

- `knowledge/business/**/*.md`（模板文件除外）默认收录；`knowledge_status` 只表达内容状态，不引入审批工作流：`observed`（观测事实）、`candidate`（候选解释）、`confirmed`（已确认口径）、`unreviewed`（默认未确认）。混合快照事实和未确认业务解释的页面使用 `candidate`。
- 文档变更通过内容摘要触发增量重建；删除文档后，对应 Vanna Documentation 在重建时移除。
- Markdown 中的表列引用必须能绑定当前 Schema Snapshot；绑定失败是构建错误，不是进入候选区等待审核。
- 当前构建校验类型、状态、快照、显式字段引用及规划摘要的 Schema/SQL 边界；它不自动证明业务含义，也不检测所有自然语言矛盾。互相冲突的定义须修订或明确适用范围，不能仅按相似度选择。
- 关系类文字可以帮助解释业务，但真正允许使用的 Join 仍以独立 Join Catalog 为准。
- 业务文档的 `candidate` 仅表示解释尚未确认，与 Agent Memory 的候选、评测、发布生命周期独立；关系候选页仍不授予 Join 权限。

## 文档约定

- 一个页面只描述一类业务概念，`page_id` 永久稳定且不带版本号。
- `business_kind` 使用 `entity`、`dimension`、`metric`、`relationship` 或 `data_quality`。
- 正文写业务定义和数据绑定，但不放完整 SQL；完整正确 SQL 走 Question-SQL 通道。
- 所有表列引用使用全限定名，并绑定数据库快照。
- 新文档从 `knowledge/business/TEMPLATE.md.example` 复制；示例模板不会被 Markdown 扫描器当作知识页。
- 单用户模式不要求 `allowed_principals`；历史页面保留该字段时仅作兼容元数据，不参与授权。

## 规划用业务说明与物理绑定

每个业务术语片段可写一段纯业务说明，供 Query Planning 使用；完整正文和物理绑定仍提供给 Schema Grounding：

```markdown
# 已分类案例数

<!-- planning -->
分类记录中出现的编码数与主档内已分类案例数是两个指标，须明确统计范围。
<!-- /planning -->

分类表编码数绑定 `t_casedesc.c_caseCode`；主档范围还需要有效关系支持。
```

每个片段最多一段规划说明，不得含表名、字段名、SQL 或预先算好的物理答案。构建时检查物理标识与 SQL；运行时再次检查，保留原有隔离边界。`planning_content` 与完整正文共享同一证据 ID、文档版本和内容状态。旧文档没有这段说明时，仅在完整正文不含物理标识或 SQL 的情况下提供给规划角色。

## 原始注释质量

`knowledge/schema_annotations.json` 记录与快照绑定的可疑注释。条目只支持 `unreliable` 状态及原因，不猜测替代字段含义。构建时将对应表摘要和列说明显示为“注释待确认”，原始注释保留在快照及语料结构化信息的 `raw_comment` 中；不进入检索正文。清单修改会改变索引版本，未知字段或快照不匹配会使构建失败。

Schema 中的空表和语义未明确的参数列仍可用于查询物理结构，不能据此推断业务指标。观测值以 JSON 数组表示，保留含分隔符的原始类别、空串及空格，并单列 NULL 行数；不将观测值当成永久枚举。

## 构建

默认 Vanna 构建输入是：

```text
artifacts/text2sql/schema/database_snapshot.json
knowledge/business/**/*.md
本地已确认 Question-SQL
```

执行：

```bash
python scripts/build_text2sql_vanna.py
```

当前项目只保留 VannaCorpus 检索链路。业务 Markdown 使用 `evoagent/text2sql/business_documents.py` 解析；重建命令为 `python scripts/build_text2sql_vanna.py`。
