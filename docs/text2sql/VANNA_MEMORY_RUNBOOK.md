# Text2SQL 会话记忆与 Vanna 检索

## 1. 运行拓扑

```text
用户问题
   |
   v
Leader（Query Router + 会话上下文）
   |-- RESULT_QA ------> 读取已授权 QueryRun 快照，Leader 直接回答
   |
   `-- DATA_QUERY / FOLLOW_UP_QUERY
           |-- Grounding Agent -- Wiki + KnowledgeStore + Vanna 检索
           `-- Strategy Agent  -- Wiki + KnowledgeStore + Vanna 检索
                        |
                     Leader 交叉检查
                        |
                     Critic 盲审
                        |
                     Harness：AST / EXPLAIN / SQLite 只读执行
```

这不是把 Multi-Agent 改成固定工作流。Leader 仍负责路由、委派、返工和最终候选选择；Harness 只是不可绕过的确定性安全边界。

## 2. 三类记忆

- 会话记忆：保存用户与会话维度的 QueryRun、独立问题、路由类型、SQL、结果摘要和最多 50 行结果快照，用于追问和结果问答。
- 知识记忆：KnowledgeStore 是权威层，保存 Schema、Wiki、关系、取值和审核后的 Question-SQL，继续执行状态、ACL、快照和版本校验。
- Skill 记忆：四个角色的策略、失败归因与稳定/候选 Memory 仍由原 EvoAgent 自进化模块管理，不与 Vanna 混合。

使用阿里云模型时，Leader 路由会收到最近 QueryRun 的问题、SQL 与结果元数据；`RESULT_QA` 还会收到所引用 QueryRun 的列名及最多 50 行有限结果快照。完整 SQLite 文件不会上传，历史快照也不会提供给其他用户或会话。

## 3. Vanna 的权限边界

`VannaRetrieverOnly` 只暴露三类检索：DDL、Documentation、Question-SQL。包装层显式封锁 `ask`、`generate_sql`、`submit_prompt` 和 `run_sql`；数据库连接也不会交给 Vanna。

Vanna 命中不能直接成为事实。每个向量条目携带 KnowledgeStore 的 `evidence_id`，返回后必须重新检查：

1. 条目仍为 `stable`；
2. 数据库快照仍一致；
3. 当前 Principal 仍满足 ACL；
4. 角色视图允许使用该知识类型。

任何检查失败都丢弃命中。索引缺失、依赖不可用或版本不匹配时，系统自动回退到 KnowledgeStore 原检索。

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
6. Skill 候选仍走 240 题 Validation/Sealed Holdout、Shadow、Canary 和人工激活流程；经验索引与 Skill 发布是两条独立演进线。

## 6. 可观测性

前端“运行轨迹”显示 Query Router 类型、父 QueryRun、独立问题、SchemaPlan、QuerySpec、Vanna 检索批次、Agent 轨迹、版本固定和 Harness 结果。“数据与知识”显示 Vanna 是否可用、条目数量以及生成/执行能力保持关闭。
