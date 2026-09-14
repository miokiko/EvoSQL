# 框架层修改说明

当前构建：`text2sql-agentic-build-v11`；门禁版本：`text2sql-harness-gates-v6`；Python 包版本：`0.5.0`。固定 11 节点的主链协议仍为 `plan-first-text2sql-v3`。

v11 在不改变节点拓扑的前提下，补充了 append-only QueryTrace revision、`ExperienceMemory/v1`、prompt-only Policy 编译、来源 Target Replay 与发布门禁血缘校验。完整边界见 [Memory 自进化 MVP 定稿](text2sql/MEMORY_EVOLUTION_MVP_PLAN.md)。

## 修改目标

增强第 2 个 Evidence Orchestration 节点的 Schema 覆盖，同时保持五种 Agent、后续 9 个节点、只读 SQL 校验和节点 Checkpoint 不变。

## Node 2 正向与反向 Schema Linking

正向阶段并行使用两类候选来源：一次受限 LLM Schema Linking，以及基于问题中表名、字段名、字段注释和值的确定性关键词匹配。两者都只产生候选，所有物理标识符必须通过固定 Snapshot 校验。

随后 Harness 从 Vanna 冻结一次 DDL、业务文档和已确认 Question-SQL 上下文，交给无工具、无数据库连接的请求级组件生成一条不可信草稿 SQL。草稿永不执行，只由 SQL AST 解析器反向提取投影、过滤、分组、排序、Join 表列；同名字段所属表由 Snapshot 确定性扩展。出现未解析、歧义、同名多表或星号投影时，再进行一次补充 Vanna 检索。

最终仍向后续节点暴露原有三路输入：`SchemaLinkPack/v3`、`GroundingPack/v1` 与 `PlanningBusinessPack/v1`。Query Planning 看不到草稿、DDL 和物理绑定；Schema Grounding 负责纠正候选；第 7 节点之后的 ApprovedQueryPlan、SQL Generation、Critic 与最终执行门禁未改变。正常单候选路径因此由 6 次 Agent 模型调用增加为 6 次 Agent 调用加 2 次非 Agent 组件调用。

## 单候选选择

第 10 个节点读取 Critic 接受的候选索引。零个通过时停止；恰好一个通过时由 Harness 直接选择；多个通过时继续调用 Lead，并严格校验返回索引。

`collaboration.lead_final.selection_method` 分别记录 `deterministic_single_candidate`、`lead_multiple_candidates` 或 `skipped`。网页轨迹会展示实际执行主体。正常、没有修订和额外工具轮次的单候选查询由 7 次模型调用变为 6 次。第 11 节点仍重新检查批准计划、SQL AST 和计划一致性，通过后才执行。

Critic 的提示词现在明确区分“问题是否被计划完整表达”和“SQL 是否忠实实现计划”，同时提供原始用户问题。它仍有否决权，不能改 SQL 或调用数据库工具。

## 用户澄清

新增 `ClarificationRequest/v1`，由 `query_outcome.py` 做结构校验。模型只能用 `ambiguous_intent` 或 `missing_business_definition` 描述需要用户回答的歧义，提出 1–3 个具体问题。缺少数据库证据、查询能力限制和输出格式错误仍走对应诊断。

Lead 路由、两个计划角色、Lead 计划评估和修订审批都可提出澄清。框架返回 `status=needs_clarification`、结构化 `clarification` 和 `diagnostic`，`gates.accepted=false`，没有候选生成、EXPLAIN 或执行。等待用户补充不显示为 SQL 安全拦截。

本轮请求的节点进度和澄清结果可以持久化、幂等读取。用户回复会新建请求，重新检索和规划，不把等待用户期间的数据版本假定为不变，也不把旧计划静默解冻。

```json
{
  "question": "强烈等级",
  "session_id": "原会话标识",
  "task_id": "新的任务标识",
  "clarification_task_id": "原澄清任务标识"
}
```

服务端读取并核验原请求属于同一用户、同一会话且状态为 `needs_clarification`。原问题与本轮用户补充组合为新的查询文本；助手提出的问题仅作为对话上下文，不自动成为用户授权的数据库事实。合计文本限制为 2000 字。重复使用相同任务标识但更改补充内容或关联任务会被拒绝。

网页提供澄清问题、补充输入和“改为新问题”按钮。等待状态在当前浏览器会话中保存，刷新后仍可回复；网络失败重试保持同一请求标识，退出登录清除待回复状态。

## 失败归因

`QueryDiagnostic/v1` 按最早发生的阻断阶段归因，包含 `stage`、`category`、`code`、`message`、`suggested_action`、`retryable` 和 `related_codes`。当前分类覆盖意图歧义、上下文认证失败、角色输出失败、证据缺口、绑定歧义、契约错误、明确的能力限制、计划拒绝、候选校验、Critic 和最终选择。

归因依据已有运行状态和错误码，不表示人工确认的根因。特别是 `unsupported_query_contract` 可能来自格式或形状不一致，只有错误消息明确指向能力限制时才归入 `unsupported_query`。

计划未获批准时，SQL Generation 标记为 `skipped`，保留 `skipped_reason=approved_plan_unavailable`。原有 Gate 错误仍保留供审计，网页优先展示根因摘要。历史轨迹也返回澄清与诊断字段。

评测新增 `NEEDS_CLARIFICATION`、`clarification_count`、`failure_stage_counts` 与 `diagnostic_category_counts`。普通单轮、有标准 SQL 答案的题如果未回答，仍计入准确率分母，不能借澄清提高成绩。Shadow 对澄清问题本身做指纹比较，避免将不同的澄清请求视为相同结果。

## 验证与边界

后端回归使用模拟模型、测试检索语料和真实只读 SQLite，覆盖单/多候选选择、严格索引校验、各规划阶段澄清、Checkpoint 复用、跨用户/会话拒绝、补充回复、新任务身份、历史轨迹与评测统计。前端使用语法检查和状态交互测试。

本次实测：**313 项 Python 测试通过**；`node --check` 和 Node 前端状态测试通过。业务文档与评测集未改写；Evolution SQLite 由幂等迁移补充 Memory/Replay 控制面结构。

```bash
python -m unittest discover -s tests
node --check web/app.js
node tests/test_framework_ui.cjs
```

没有运行真实模型全量 Benchmark、真实向量服务集成或 Docker 构建，也没有证明准确率提升。当前运行环境没有浏览器可执行文件，前端交互测试使用 Node 模拟 DOM 和请求，不代表真实浏览器视觉验证。

双角色规划合并、取消 Critic 等方案仍需要固定模型、数据、RAG 与记忆版本的对照实验后再决定。当前版本没有移除任何机器安全门禁，也没有扩大查询语言的能力范围。

升级后，旧构建的 Checkpoint 身份不匹配会停止恢复；使用新任务标识。原有历史数据和评测工件继续用于回溯，不作为 v9 的效果成绩。

## 工程目录

本次交付为普通工程目录。`project-empty-files.json` 记录原工程中 8 个零字节文件，`python scripts/restore_project_files.py` 可以在复制工程后补齐这些空日志、WAL 和索引占位文件，已有文件（包括后续写入内容的 WAL）不会被覆盖。非空源码、数据库、索引和评测工件均按原目录保留。
