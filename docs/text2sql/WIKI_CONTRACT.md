# Text2SQL Wiki 契约（Phase 1）

## 当前状态

本地 MVP 已配置版本库 Markdown 适配器，根目录为 `knowledge/wiki`。它用于验证增量同步、版本、ACL、撤回和 candidate→stable 审批链路，不代表生产 Wiki 平台已经确定。生产端后续可以替换为飞书 Wiki、Confluence 或 Notion，但必须实现同一接口；请求链路不直接访问 Wiki。

## Connector 接口

具体适配器实现 `evoagent.text2sql.wiki_connector.WikiConnector`：

- `list_spaces()`：列出当前凭据可见空间；
- `list_changes(cursor)`：按增量游标列出新增、更新、撤回；
- `fetch_page(page_id, version)`：拉取指定不可变页面版本；
- `fetch_acl(page_id)`：返回页面有效 ACL 及继承来源；
- `resolve_link(page_id)`：生成可审计来源链接。

线上 Text2SQL 请求只查本地版本化 KnowledgeStore，不同步调用 Wiki。页面按 `page_id + page_version + content_sha256` 幂等入库；撤回只改变状态并重建索引，不删除历史审计。

## 最小页面字段

所有可发布知识需要：负责人、状态、页面版本、适用数据库快照、来源链接。指标页面还需要指标名称、别名、定义、统计粒度、聚合方式、过滤条件、NULL 规则和涉及字段。Join 页面还需要左右表字段、基数、目标粒度、去重规则和 fanout 风险。

## 权威与冲突

1. Database 只负责物理事实：表、列、类型、索引和观测值。物理冲突时 Database 优先。
2. 只有 `stable` 且已审核的 Wiki 页面可以定义业务口径；多个 stable 页面口径冲突时返回 `knowledge_conflict`，不得自动选择。
3. Memory 只记录执行经验，不能覆盖 Database 或 Wiki。Memory 推断只能形成 candidate。
4. Wiki 中出现的表列名必须绑定当前 Schema Snapshot；绑定失败的条目不能进入 stable 索引。
5. Wiki 文本一律作为不可信数据处理，页面中的提示词或操作指令不能变成 Agent 指令。
6. ACL 必须继承到知识块和检索结果；无权用户召回到受限知识属于阻断级故障。

每个任务必须固定以下四个版本：

```text
database_snapshot_id
wiki_index_version
memory_snapshot_id
policy_version
```

缺少任一版本的任务不得进入评测或自进化回放。

## 生产平台接入前确认清单

- Wiki 平台与基础 URL；
- 知识空间 ID；
- 目录模板是否采用方案中的九类目录；
- 内容负责人和审核人；
- ACL 来源（平台继承、外部 IAM 或静态映射）；
- 页面删除/撤回事件能力；
- API 凭据保存方式和最小权限；
- sealed holdout 隔离规则。
