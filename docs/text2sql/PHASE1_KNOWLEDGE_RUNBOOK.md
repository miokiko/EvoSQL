# Text2SQL Phase 1：知识底座运行手册

## 已实现边界

这一阶段只建设 Text2SQL 的可信知识层，不生成或执行 SQL，也不启用自进化。数据来自本项目的独立 SQLite 评测库和本项目原创的 Markdown Wiki 页面，没有读取或复用“匿名审核员-text2sql”的 Agent、Prompt、检索、记忆或评测代码。

知识分为五类：`schema`、`business_glossary`、`value`、`relationship`、`verified_example`。状态门禁如下：

| 来源 | 初始状态 | Agent 是否可见 |
|---|---|---|
| 当前数据库 Schema | `stable` | 是 |
| 当前数据库低基数值域 | `stable` | 是 |
| 自动推断 Join | `candidate` | 否，人工批准后才可见 |
| Markdown Wiki | `candidate` | 否，人工批准后才可见 |
| 校验失败或含提示词注入的 Wiki | `quarantined` | 否，且不能批准 |
| 被替换或撤回的页面版本 | `revoked` | 否，保留审计记录 |

## 构建知识库

在项目根目录执行：

```bash
python scripts/build_text2sql_knowledge.py
```

默认输入：

- `artifacts/text2sql/schema/database_snapshot.json`
- `artifacts/text2sql/schema/join_catalog.review.json`
- `knowledge/wiki/**/*.md`

默认输出：

- `artifacts/text2sql/knowledge/knowledge.sqlite3`
- `artifacts/text2sql/knowledge/manifest.json`

同步以页面清单游标和内容 SHA-256 判断变更。重复构建不会产生重复知识项；同步运行日志会保留新的审计时间。

## 检索角色视图

```bash
python scripts/query_text2sql_knowledge.py \
  "强烈岩爆案例有多少个" \
  --role schema-grounding \
  --principal local-user
```

当前 `plan-first-text2sql-v3` 运行协议包含五个 Agent：`text2sql-lead`、`schema-grounding`、`query-planning`、`sql-generation`、`text2sql-critic`。实际请求由 Evidence Orchestration 统一读取 stable 证据：Schema Grounding 可接收物理 Schema、值域、关系和已审核样例；Query Planning 保持 schema-blind，只接收问题和已审核的 `business_glossary`；SQL Generation 只消费 Lead 审核、Harness 铸造的 `ApprovedQueryPlan`；Blind Critic 只审核通过 Harness 候选门禁的匿名候选。Harness 负责确定性绑定、SQL Gate 和只读执行，不是 Agent，也不是 Skill。

上面的 `query_text2sql_knowledge.py` 是 Phase 1 保留的独立只读检查入口，其 `--role` 仍暴露历史检索视图名 `lead`、`schema-grounding`、`sql-strategy`、`critic`。其中 `sql-strategy` 仅表示旧版检索权重视图，不是当前 Agent 或可演化 Skill，也不能用于新 Policy/Memory 写入。无论选择哪个兼容视图，KnowledgeStore 都共同遵守三个硬门禁：只读取当前数据库快照、只读取 `stable`、只返回调用者 ACL 允许的内容。

返回的 `EvidencePack` 固定：

```text
database_snapshot_id
wiki_index_version
memory_snapshot_id
policy_version
```

每条证据包含 `evidence_id`、来源版本、依赖表列和得分。中文检索先定位字段和值，再用已批准的 Join Graph 做小范围扩展；candidate Join 不参与扩展。

## 审批与撤回

列出待审项：

```bash
python scripts/review_text2sql_knowledge.py list
```

批准或拒绝单条知识：

```bash
python scripts/review_text2sql_knowledge.py approve <evidence_id> \
  --reviewer <审核人> --reason <依据>

python scripts/review_text2sql_knowledge.py reject <evidence_id> \
  --reviewer <审核人> --reason <原因>
```

批准会发布新的 stable 索引版本；拒绝会保留审核记录。Wiki 页面删除后再次构建，会把该页面所有 active 知识置为 `revoked`。编辑已批准页面会撤回旧版本并创建新 candidate，新内容不会继承旧版本的批准状态。

## 编写 Markdown Wiki 页面

复制 `knowledge/wiki/TEMPLATE.md.example`，保存为 `.md`。每页至少填写：

```yaml
page_id: globally-unique-page-id
title: 页面标题
owner_id: 负责人
allowed_principals:
  - team-or-user-id
knowledge_type: business_glossary
database_snapshot_id: dbs_57a4a4c99520477b1c8e
```

正文引用真实字段时使用完整名称，例如 `t_casedesc.c_rockLevel`。未知表列、数据库快照不匹配、缺少 owner/ACL 或检测到提示词注入时，页面进入 `quarantined`。

`allowed_principals: ["*"]` 仅适合本地公开样例；生产 Wiki 必须从平台有效 ACL 继承用户或组身份。

## 替换为生产 Wiki

实现 `evoagent.text2sql.wiki_connector.WikiConnector` 的五个方法即可替换 Markdown 适配器：空间发现、增量变更、不可变页面版本、有效 ACL 和审计链接。线上 Text2SQL 请求仍然只访问本地 KnowledgeStore；平台同步作为独立任务运行，避免权限漂移和外部服务抖动进入请求链路。

## 验证

```bash
python -m unittest tests.test_text2sql_knowledge -v
```

测试覆盖增量游标、幂等同步、ACL 隔离、页面撤回、提示词注入隔离、未知字段隔离、candidate/stable 分离、中文值域召回和已审批 Join 扩展。
