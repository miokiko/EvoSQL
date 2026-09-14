# 项目清理记录

清理日期：2026-09-05。本记录对应清理时的构建 `text2sql-agentic-build-v8`、Python 包版本 `0.4.0`。后续 v9 框架层修改见 [框架层修改说明](FRAMEWORK_CHANGES.md)。

## 已完成

- 删除 PR Review 的服务、GitHub 回调、代码扫描、Diff/Hunk 分析、修复、队列、PR 评测和 PR 策略演化代码，以及只服务于它们的 Skills、脚本、测试、数据库和工件。
- 删除旧 KnowledgeStore、Wiki Connector、知识审批与构建命令、旧知识数据库及不含 corpus sidecar 的旧索引。Question-SQL 的旧候选知识评测/激活接口、后台任务和空表也已移除。
- 将共享的有界角色执行器拆到 `evoagent/bounded_role.py`；通用上下文处理仅保留 Text2SQL 所需的预算、结构化对象与工具 Observation 处理。压缩时保留计划、SQL 和候选索引，无法放入预算则停止。
- 新增 `ApplicationService` 与 `AuthStore`，Web 启动不再构造 PR 服务。查询、反馈、轨迹、Memory / Policy 治理、登录与审计仍可使用。网页支持登录和退出。
- 将业务 Markdown 解析和校验提取为 `business_documents.py`，由 VannaCorpus 直接消费；仍校验 frontmatter、注入模式、Schema 引用和数据库快照。
- 清理 PostgreSQL、Redis、PR 相关配置和依赖；MySQL 导入核验保留为可选 Compose profile 与独立依赖文件。
- 同步 README、运行手册、当前设计说明和简历描述。个人导学/面经笔记保留原文并标注为历史材料，不作为当前实现依据。

## 保留的数据与能力

数据库 dump、只读 SQLite、Schema Snapshot、业务 Markdown 和 240 题评测集均保留；数据库目录、评测集和业务文档的文件内容与上传版本逐字节一致。

Vanna corpus 的确定性重算版本仍为 `vanna-7ea7c3091219`，与随附当前索引一致。Working / Episodic / Semantic Memory、计划绑定、Blind Critic、只读执行、Checkpoint、Memory 评测及 Policy Shadow / Canary / 回滚继续保留。

保留了旧 Policy、Trace 和版本字段的必要读取兼容：这些不会加载已删除的 PR 或知识数据库模块。`wiki_index_version` 仍是传输字段，值代表 Vanna corpus。

## 本次验证

- `python -m unittest discover -s tests`：**212 tests passed**。
- 新增独立 Web 启动、登录与会话传递、反馈审计、已删除接口返回 404、业务文档校验、结构化上下文保护测试。
- 原有 Text2SQL 主链测试已迁移到 VannaCorpus；使用脚本模拟模型输出、模拟向量写入与真实只读 SQLite，覆盖正常执行、角色证据隔离、计划修订、SQL 修复、Checkpoint 和发布治理。
- Python 源码编译、项目内部导入路径检查、主要 CLI 参数检查、`node --check web/app.js` 均通过；无指向已删除模块的源码导入。
- 测试环境为 Python 3.12.13、SQLGlot 30.18.0。未调用付费模型；未进行真实 Chroma 向量服务集成测试。当前环境无 Docker，未实际构建 Docker 镜像。

测试通过说明被覆盖的工程行为有效，不代表当前架构的真实模型 EX 成绩，也不证明自进化已经提升准确率。

## 使用清理版

1. 解压到新目录，避免将新文件覆盖到仍含旧 PR 模块的目录。
2. 建立新的虚拟环境，执行 `python -m pip install -r requirements.txt`。MySQL 核验另用 `requirements-mysql.txt`。
3. 配置本地 `.env`。压缩包不包含真实 API 密钥、原虚拟环境和 Git 元数据；可从原目录迁移仍在使用的模型配置。`.env.example` 默认可打开无模型的控制台。
4. 如需重建数据，按 README 执行 Schema / SQLite / Vanna 构建命令，再运行 `python -m evoagent`。
5. 新的认证库默认位于 `artifacts/text2sql/auth.sqlite3`，不读取已删除的 `evoagent.db`。上传版本中的用户和成员记录均为空。
6. 构建版本已变化，旧版本 Checkpoint 会拒绝恢复；为新查询使用新的 task_id。原历史查询和评测工件继续保留以便追溯。

## 删除的 Python 文件

- `evoagent/agentic_core.py`
- `evoagent/diff_parser.py`
- `evoagent/evaluation_benchmark.py`
- `evoagent/evaluation_harness.py`
- `evoagent/evaluation_v2.py`
- `evoagent/evolution.py`
- `evoagent/evolution_proof.py`
- `evoagent/evolution_v2.py`
- `evoagent/fixer.py`
- `evoagent/gates.py`
- `evoagent/github.py`
- `evoagent/harness.py`
- `evoagent/memory.py`
- `evoagent/models.py`
- `evoagent/modes.py`
- `evoagent/observability.py`
- `evoagent/patching.py`
- `evoagent/postgres_store.py`
- `evoagent/report.py`
- `evoagent/repository_tools.py`
- `evoagent/reviewer.py`
- `evoagent/rollout.py`
- `evoagent/service.py`
- `evoagent/skill_evolution.py`
- `evoagent/skills.py`
- `evoagent/store.py`
- `evoagent/task_queue.py`
- `evoagent/text2sql/knowledge_ingestion.py`
- `evoagent/text2sql/knowledge_policy.py`
- `evoagent/text2sql/knowledge_store.py`
- `evoagent/text2sql/markdown_wiki.py`
- `evoagent/text2sql/wiki_connector.py`
- `evoagent/verifier.py`
- `scripts/build_text2sql_knowledge.py`
- `scripts/import_github_pr_dataset.py`
- `scripts/query_text2sql_knowledge.py`
- `scripts/review_text2sql_knowledge.py`
- `scripts/run_agentic_evaluation.py`
- `scripts/run_prompt_evolution_proof.py`
- `scripts/run_real_pr_benchmark.py`
- `scripts/run_text2sql_experience_evaluation.py`
- `tests/agentic_fake.py`
- `tests/test_advanced.py`
- `tests/test_agentic_evaluation.py`
- `tests/test_diff_parser.py`
- `tests/test_evaluation_harness.py`
- `tests/test_evolution_proof.py`
- `tests/test_github.py`
- `tests/test_harness.py`
- `tests/test_lead_worker_collaboration.py`
- `tests/test_phases_0_5.py`
- `tests/test_production_features.py`
- `tests/test_reviewer.py`
- `tests/test_runtime_memory_context.py`
- `tests/test_service.py`
- `tests/test_skill_evolution.py`
- `tests/test_text2sql_knowledge.py`
