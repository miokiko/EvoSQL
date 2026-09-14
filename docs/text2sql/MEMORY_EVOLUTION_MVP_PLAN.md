# EvoSQL Memory 分层沉淀与 Policy 自进化 MVP 实施计划

版本：v1.1 Implemented  
日期：2026-09-07  
状态：人工治理链与受限自动链均已实现；真实模型 Target Replay 与 96 条发布评测待首个真实候选执行  
适用基线：`text2sql-agentic-build-v18` / `plan-first-text2sql-v3`

实施验收（2026-09-06）：313 项 Python 回归、JavaScript 语法检查和前端状态测试通过；本地服务已完成幂等数据库迁移并返回 `ready=true`。当前库没有 Confirmed Semantic Experience 或 Experience-driven Policy Candidate，因此没有伪造 Target Replay、96 条发布评测、Shadow、Canary 或激活结果；这些步骤会在首个真实候选产生后按本计划执行。

## 1. 目标

在不改变现有五个 Agent、十一节点拓扑和 Harness 权限边界的前提下，补通下面这条最小闭环：

```text
QueryRun
  → 有证据的 Experience Candidate
  → 人工确认的 Experience
  → 单 Agent Policy Candidate
  → 定向验证与现有发布闸门
  → Shadow / Canary / 显式激活
  → 后续 QueryRun 固定新 Policy 版本
```

本 MVP 解决两个问题：

1. Memory 分层能够真实沉淀，而不是只有页面概念和少量手工记录。
2. 已确认经验能够成为 Policy 自进化的输入，但不能直接改变线上行为。

## 2. 最终设计决策

### D1. Memory 与 Policy 分开

- Memory 保存经验：什么情况下出了问题、如何修正、有哪些证据。
- Evolution 聚合经验、生成候选策略并组织验证。
- Policy 是部署产物：某个 Agent 在一个固定版本内必须遵守的行为。

Policy 不是 Memory 的下一层，也不是 Memory 的一个状态。

### D2. 类型分层与生命周期分开

Memory 类型分层：

```text
Working Memory   当前会话消息
Episodic Memory  一次完整 QueryRun
Semantic Experience  有证据的 Agent 行为经验
```

Semantic Experience 生命周期：

```text
candidate
  ├─ confirmed
  ├─ rejected
  └─ needs_evidence
```

`used_in_policy` 不是 Experience 状态，而是通过 Policy 来源关系计算出的使用情况。一条 Experience 可以被多个后续 Policy 版本引用。

### D3. MVP 不建设完整 Event Store

首版直接使用现有 `query_traces` 作为 Episodic Source。它已经包含问题、Node 2 证据、两个 Plan Worker 输出、绑定与修订、ApprovedQueryPlan、SQL 候选、Gate、执行结果和版本 Pin。

以下能力延后到可靠性增强版本：

- 全节点 append-only RunEvent；
- Event Store 与 QueryTrace 投影器；
- 通用后台提炼队列；
- Worker lease/fencing；
- Checkpoint 到事件的恢复扫描；
- 独立 Evidence Link 多对多表。

### D4. 新 Experience 不直接注入 Agent

新流程产生的 Experience 一律不可作为动态 Prompt Memory 使用：

```text
runtime_eligible = false
```

线上行为只能通过激活后的 Policy 改变。现有 Legacy Stable Memory 保持只读兼容；当前本地库没有 Memory 项，因此不需要迁移历史 Stable 内容。

### D5. 自动发布仅限机器可验证 Experience 链

系统可以自动：

- 识别高置信修订；
- 形成 Experience Candidate；
- 对机器可验证 Experience 做证据准入，并生成离线 SemanticRule 与同 Agent Policy Candidate；
- 执行确定性校验和评测。
- 在 Target Replay、完整 Validation/Holdout 门禁和发布身份复核全部通过后原子激活。

系统不能自动：

- 接纳纯文字反馈、缺少修订前后证据或无法唯一归因的 Experience；
- 扩大 Agent 工具权限；
- 修改知识事实或 Join Catalog；
- 绕过 Target Replay、离线评测或版本/身份/来源校验；
- 自动发布普通人工 Prompt 候选，或自动回滚 Policy。

默认生产发布仍保留 Shadow、Canary 和人工审批；本地自动 MVP 在完整离线门禁后可跳过这三步，适用于验证自动演化技术闭环，不等价于无人治理的生产发布。

### D6. 首版 Policy 自进化只允许修改 `prompt_fragment`

自动进化首版只允许修改一个目标 Agent 的 `prompt_fragment`。以下字段不进入自动进化：

- `field_aliases`、`value_aliases`：属于 Knowledge；
- `few_shot_examples`：Question-SQL 继续由 Vanna 管理；
- `allowed_tools`：属于权限治理；
- `budget_parameters`：属于运行治理；
- deterministic Gate：属于代码资产。

## 3. 与现有系统的边界

### 3.1 保持不变

- 五个 Agent：Lead、Schema Grounding、Query Planning、SQL Generation、Critic；
- 十一节点运行拓扑；
- Node 2 的正向 LLM 链接、关键词链接、Vanna 检索、草稿反向提取和 Schema 补充；
- Schema Snapshot、业务 Markdown、Join Catalog 和 Vanna 的事实边界；
- `PolicyArtifact/v2`、单 Agent 变更校验和版本哈希；
- Policy 离线评测、Shadow、Canary、显式激活和回滚；
- Confirmed Question-SQL 的 `experience_reviews → Vanna` 路径；
- Checkpoint 的运行恢复和身份校验。

### 3.2 Node 与 Agent 的边界

Node 2 的 forward linker 是有界组件，不是第六个 Agent。

- 它的输入输出保存在 Episodic QueryTrace 中；
- 它本身不消费 Semantic Experience；
- 如果后续计划修订证明存在 Schema 绑定问题，经验 owner 可以归因到 Schema Grounding Agent；
- Node 2 正常发生补充检索，不自动视为错误或经验。

### 3.3 Knowledge、QSQL 与工程问题

MVP 只沉淀 Agent 行为经验，不把其他领域重新包装成 Memory：

| 内容 | 去向 |
| --- | --- |
| Agent 分析、检查、修复方法 | Semantic Experience |
| 用户确认的问题—SQL | `experience_reviews` 与 Vanna |
| 字段别名、枚举、业务口径 | Knowledge 修订，MVP 暂不自动处理 |
| Join 路径和基数事实 | Join Catalog 复核，MVP 暂不自动处理 |
| 超时、数据库故障、框架限制 | 工程日志或工程事项 |

## 4. MVP 总体链路

```text
Web / CLI 查询
      ↓
现有 11 节点 Text2SQL 运行
      ↓
共享 finalize_run：保存 QueryTrace
      ↓
确定性 Experience Extractor
      ├─ 无高置信修订 → 结束，不生成 Candidate
      ├─ 证据不足 → needs_evidence
      └─ 证据完整 → Experience Candidate
                              ↓
                         人工确认
                              ↓
                    Confirmed Experience
                              ↓ 人工选择同一 Agent 的一条或多条
                    Policy Candidate Generator
                              ↓
                    现有 PolicyArtifact/v2 Candidate
                              ↓
                    来源案例定向 Baseline/Candidate 验证
                              ↓
                    validation + sealed_holdout 发布评测
                              ↓
                    Shadow → 人工差异审核 → Canary
                              ↓
                    显式激活 / 可回滚
```

## 5. Episodic Memory：统一 QueryTrace

### 5.1 记录入口

将 Web 中现有 `_remember_trace()` 提取为 Text2SQL 专用共享服务：

```python
finalize_run(
    result,
    internal,
    *,
    task_id,
    user_id,
    session_id,
    origin,
) -> MemoryWriteStatus
```

Web、生产 CLI、调试和评测入口使用相同记录方法。来源拆成两个维度。

`origin` 区分调用入口：

- `web`
- `cli`
- `debug`
- `evaluation`

`source_lane` 区分发布流量：

- `stable`
- `shadow`
- `candidate`（包含 Canary 的候选分支）

只有实际服务用户的 stable lane 可成为生产经验来源；evaluation、shadow 和 candidate lane 不能冒充新的生产案例。

### 5.2 成功、澄清和失败

- 成功：保存完整 QueryTrace。
- 澄清：保存问题缺口和可用中间产物，不生成失败经验。
- 运行异常：保存最小失败 Trace/Attempt，包括最后节点、错误类别和版本 Pin；基础设施错误不生成 Agent Experience。
- 重试：同一 `task_id + source_revision` 幂等更新，不重复生成 Experience。

### 5.3 写入不能改变查询结果

Trace/Experience 写入属于旁路：

- 写入成功：返回 `memory_status=recorded`；
- 写入失败：原 SQL/答案照常返回，并附 `memory_status=degraded`；
- 不允许因为沉淀失败把已成功查询改成 API 错误。

### 5.4 保留策略

- 页面仍只展示最近 50 条；
- 移除 `save_query_trace()` 中每会话超过 50 条后的物理删除；
- MVP 阶段不自动删除 QueryTrace；
- 结果行继续有界保存并标记 `truncated`；
- 不保存完整模型 Prompt、隐藏推理、密钥或凭据。

## 6. Semantic Experience 合同

新写入采用 `ExperienceMemory/v1`：

```json
{
  "contract": "ExperienceMemory/v1",
  "memory_id": "memory-...",
  "source_task_id": "text2sql-...",
  "source_revision": 1,
  "target_agent": "sql-generation",
  "source_stage": "candidate-gates",
  "problem_code": "sql_gate_repair",
  "scenario": "ApprovedQueryPlan 已固定，但首轮 SQL 未通过计划一致性门禁",
  "problem": "首轮 SQL 遗漏了计划要求的 DISTINCT",
  "correction": "在同一 ApprovedQueryPlan 下补齐 DISTINCT 后通过门禁",
  "applicability": {
    "approved_plan_has_distinct": true
  },
  "before": {
    "sql_fingerprint": "...",
    "gate_codes": ["distinct_mismatch"]
  },
  "after": {
    "sql_fingerprint": "...",
    "gate_accepted": true
  },
  "evidence": {
    "approved_plan_fingerprint": "...",
    "database_snapshot_id": "...",
    "vanna_index_version": "...",
    "memory_snapshot_id": "...",
    "policy_version": "..."
  },
  "evidence_grade": "deterministic_repair",
  "state": "candidate"
}
```

### 6.1 约束

- 一次来源问题中的一个独立问题生成一条 Experience；
- 不按指纹合并并增加 `occurrence_count`；
- 指纹只用于页面聚类和人工批量选择；
- Evidence 在创建后不可原地替换；
- 修改问题、修正或适用条件时创建新版本/新记录；
- 同一 `source_task_id + problem_code + evidence_sha256` 幂等；
- `derived_from_memory_ids` 仅用于防循环，不增加支持数；
- QueryTrace 使用过某条 Memory 或 Policy，不得反向为其提供独立支持。

### 6.2 存储兼容

MVP 复用现有 `memory_items`，新行使用 `ExperienceMemory/v1`，并补充最少字段：

- `source_task_id`
- `source_stage`
- `source_revision`
- `evidence_sha256`
- `runtime_eligible`，默认 `0`
- `state_version`

现有 `rule_json` 字段保存规范化 Experience JSON，`content` 只保存有界展示文本。旧 `AgentSemanticRule/v1` 通过兼容读取器保留，但新路径不再创建 Legacy Stable Runtime Memory。

## 7. Experience 自动提取

MVP 不调用新的 LLM 提炼器，只实现三个确定性来源。

### 7.1 用户明确纠错

复用现有错误反馈归因：

- 用户明确选择“结果不正确”；
- 必须填写原因；
- 只有提供修正 SQL 且通过只读和 Schema Gate，才生成可确认 Candidate；
- 只有文字原因或归因说明时生成 `needs_evidence`，避免进入无法验证的 Target Replay 死路；
- Experience 只保存原/修正 SQL 指纹、Gate 差异和反馈摘要，原始 SQL 留在对应 QueryTrace；
- Candidate 也不自动 Confirm。

### 7.2 Plan 修订成功

同时满足以下条件才生成 Experience Candidate：

1. 初始 SchemaPlan 或 QuerySpec 存在结构化问题；
2. Binder/Lead 向一个明确的 Plan Worker 发出修订请求；
3. 修订前后差异可以确定；
4. 对应问题在修订后消失；
5. 最终计划被 Harness 铸造成 ApprovedQueryPlan。

归因规则：

- 物理表列、值、Join、物理粒度 → Schema Grounding；
- 指标、维度、过滤阶段、去重、排序、结果形状 → Query Planning。

如果一次修订同时包含两类独立问题，可以形成两条 Experience；每条只能有一个 owner。

### 7.3 SQL Gate 修复成功

同时满足以下条件才生成 Experience Candidate：

1. ApprovedQueryPlan 指纹固定；
2. 首轮 SQL 候选被确定性 Gate 拒绝；
3. 只发生一次现有 SQL Generation repair；
4. 修复后候选通过相同 Gate；
5. 差异能够对应到明确的 Gate code。

该类 Experience 归因 SQL Generation。

### 7.4 明确不生成 Candidate 的场景

- 普通查询成功且没有反馈；
- Node 2 正常补充了一些字段，但没有后续错误证据；
- 正常澄清；
- LLM、网络、数据库、存储或超时故障；
- Critic/Lead 只有自报结论，没有外部或确定性证据；
- evaluation、sealed holdout、shadow candidate lane；
- 当前 Trace 已受同一 Experience 派生 Policy 影响，且没有新增独立人工证据。

## 8. Experience 审核

审核页面必须展示：

- 来源 QueryRun；
- owner Agent 与阶段；
- 问题和修正；
- 修订前后对比；
- Evidence grade 与版本 Pin；
- 是否使用过现有 Memory/Policy；
- 是否已被某个 Policy 引用。

操作：

- `confirm`：证据、归因和修正均成立；
- `reject`：必须填写原因；
- `needs_evidence`：当前无法证明修正或 owner；

Experience 的语义与 Evidence 在当前 revision 内不可编辑。需要补证或改写时，保留原记录并由同一来源生成新的 `source_revision`，不能在浏览器覆盖来源事实。

Confirmed 只表示“经验成立”，不会改变 `memory_snapshot_id`，也不会进入任何 Agent Prompt。

## 9. Memory → Policy 自进化

### 9.1 发起方式

首版采用人工选源、自动生成：

1. 人工选择一条或多条 `confirmed` Experience；
2. 所选 Experience 必须属于同一个 `target_agent`；
3. 点击“生成 Policy 候选”；
4. Evolution Generator 对经验聚类并生成目标 Agent 的完整新 `prompt_fragment`；
5. 使用现有 `PolicyArtifact/v2` 校验和 `propose_policy()` 入库。

不设置“一条反馈”或“两次出现”自动发起阈值，避免低质量候选泛滥。

### 9.2 Generator 输入

Generator 只读取：

- 选中的 Confirmed Experience 脱敏投影；
- 目标 Agent 当前 `prompt_fragment`；
- 目标 Agent 固定职责与输入边界；
- 最大长度和禁止项。

Query Planning 的生成输入必须保持 Schema-blind。任何目标 Agent 的自动候选都不得写入字段别名、值别名、SQL 示例、工具权限、预算或 Gate。

### 9.3 Generator 输出

不新增 `PolicyDelta` 存储合同。Generator 直接输出：

```json
{
  "clusters": [],
  "skill_patch": {
    "prompt_fragment": "完整的新目标角色指导"
  },
  "rationale": "本次变更要解决什么问题",
  "memory_ids": ["memory-..."]
}
```

Harness 将 patch 应用到当前 Active Policy，生成一个只修改单 Agent 的完整 `PolicyArtifact/v2` Candidate。

### 9.4 来源关系

继续使用现有：

- `proposal_metadata_json.memory_ids`
- `compiled_memory_ids`
- `compiled_memory_fields`
- `policy_source_memory_ids()`

Policy 页面根据这些关系展示“来源 Experience”。回滚 Policy 不删除 Experience。

## 10. Policy 验证与发布

### 10.1 定向来源验证

每个 Policy Candidate 先对来源 Experience 对应案例执行 Baseline/Candidate 对照：

- 使用相同数据库 Snapshot、Vanna 版本、模型配置和运行参数；
- Baseline 固定 parent Policy；
- Candidate 固定新 Policy；
- Candidate 必须消除 Experience 对应 `problem_code`；
- Candidate 不得触发新的安全、执行或计划一致性错误；
- 结果保存为有哈希的 Target Replay Artifact，并绑定 Policy Candidate。

定向验证失败时，不进入正式发布评测。

### 10.2 数据集口径

当前数据集总计 240 条：

- train：144 条；
- validation：48 条；
- sealed_holdout：48 条。

必须继续验证整套 240 条数据集的人审资格、数据集哈希和签名证书，但 Policy 的独立离线发布闸门复用当前实现：

```text
validation 48 + sealed_holdout 48 = 96 条
```

train/生产来源案例用于经验生成和定向回放，不作为独立泛化证据。可以额外生成 240 条全量回归报告，但不将 train 的结果包装成独立发布证明。

### 10.3 现有发布链

通过定向验证后，复用现有流程：

```text
Policy candidate
  → validation + sealed_holdout offline gate
  → shadow_ready
  → Shadow 双跑，始终返回 stable
  → 人工审核差异
  → Canary，小流量 candidate + stable fallback
  → canary_passed
  → 显式人工激活
  → approved Active Policy
```

激活前必须再次核对 Policy parent、Database Snapshot、Vanna index、模型、运行协议和来源 Experience 身份。运行中的请求继续使用已冻结旧版本，新版本只影响后续请求。

## 11. 页面最小改动

### 11.1 Memory 页面

保留三类展示：

```text
Working Memory
Episodic QueryRuns
Semantic Experiences
```

Semantic Experiences 分为：

- 待审核；
- 待补证；
- 已确认；
- 已拒绝。

增加：

- 修订前后 Evidence 展示；
- Confirm / Reject / Needs Evidence；
- 多选同 Agent Confirmed Experience；
- “生成 Policy 候选”按钮；
- “已用于 Policy”版本链接。

MVP 采用逐条展示；同类指纹聚合视图属于后续可用性增强，不是发布安全前置条件。

删除或隐藏新流程中的“Memory 240 题评测”和“激活 Stable Memory”入口。Legacy 数据如存在则放入只读兼容区。

### 11.2 Evolution 页面

首版只需补充：

- Policy Candidate 的来源 Experience；
- `prompt_fragment` 前后 Diff；
- Target Replay 状态与结果；
- 跳转到现有离线评测/Shadow/Canary 状态。

Policy 的复杂发布操作首版可以继续使用现有 CLI；浏览器全流程操作不是闭环成立的前提。

## 12. 数据迁移与兼容

1. 数据库迁移必须幂等。
2. 新列全部提供安全默认值。
3. 新写入只使用 `ExperienceMemory/v1`。
4. 旧 `AgentSemanticRule/v1` 仍能读取和展示。
5. 旧 Stable Memory 不自动转换成 Experience，也不自动编入 Policy。
6. 当前本地数据库没有 Memory 项，因此本轮无需内容迁移。
7. 变更运行时 Memory 输入行为时更新 BUILD_VERSION，使旧 checkpoint fail closed。
8. Policy Artifact 合同不变，无需新增 Policy 迁移协议。

## 13. 实施阶段

### P0：契约和兼容迁移

交付：

- `ExperienceMemory/v1` normalize/decode/fingerprint；
- `memory_items` 最小字段迁移；
- 新状态和 Legacy 读取适配；
- Confirmed Experience 查询；
- `runtime_eligible=false` 强制检查；
- 数据脱敏、大小上限和幂等约束。

主要文件：

- `evoagent/text2sql/evolution.py`
- `evoagent/text2sql/memory_attribution.py`
- `tests/test_text2sql_semantic_memory.py`
- `tests/test_text2sql_evolution.py`

### P1：统一 QueryTrace 与自动提取

交付：

- 新增 Text2SQL 专用 `memory_service.py`；
- Web/CLI 统一 `finalize_run()`；
- 写入失败不改变查询响应；
- 取消 QueryTrace 50 条物理删除；
- 用户纠错、Plan 修订、SQL Gate 修复三个 Extractor；
- 相同来源重复处理保持幂等。

主要文件：

- `evoagent/text2sql/memory_service.py`
- `evoagent/text2sql/web_service.py`
- `evoagent/text2sql/evolution.py`
- `evoagent/text2sql/memory_attribution.py`
- `scripts/run_text2sql.py`
- `scripts/debug_text2sql_query.py`

### P2：审核页面与 Policy 桥接

交付：

- Memory Candidate 的 Evidence 审核；
- Confirmed Experience 多选；
- Policy Generator 改为读取选中的 Confirmed Experience；
- 自动输出仅含 `prompt_fragment` 的单 Agent Candidate；
- Policy 来源 Experience 绑定；
- Memory/Evolution 页面最小展示。

主要文件：

- `evoagent/text2sql/policy_generator.py`
- `evoagent/text2sql/evolution.py`
- `evoagent/text2sql/web_service.py`
- `evoagent/api.py`
- `web/app.js`
- `web/app.css`

### P3：定向验证与端到端验收

交付：

- 来源案例 Baseline/Candidate 定向回放；
- Target Replay Artifact 与 Candidate 绑定；
- Replay 来源必须逐条证明已编译到目标 Agent 的 `prompt_fragment`；
- 复用现有 96 条 Policy 发布评测；
- Shadow/Canary/激活/回滚回归；
- 运行身份和 checkpoint 失效测试；
- 最小运行手册更新。

主要文件：

- `scripts/run_text2sql_evaluation.py`
- `scripts/manage_text2sql_evolution.py`
- `evoagent/text2sql/evolution.py`
- `evoagent/text2sql/shadow.py`
- 对应 tests 与运行手册

## 14. 必须通过的 MVP 验收

### A01：Web/CLI 统一记录

同一类查询从 Web 和 CLI 执行，都产生可查看 QueryTrace，入口标签不同。

### A02：旁路失败不影响答案

模拟 Memory SQLite 写入失败，查询成功结果仍返回，`memory_status=degraded`。

### A03：普通成功不生成 Experience

没有反馈、修订或修复的正常 QueryRun 只进入 Episodic Memory。

### A04：用户纠错生成 Candidate

用户明确判错、填写原因并提供通过 Gate 的修正 SQL 后，生成一条可回放 Candidate；只有文字原因时生成 `needs_evidence`。

### A05：Plan 修订生成正确 owner

Grounding 物理绑定修订归因 Schema Grounding；逻辑去重/粒度修订归因 Query Planning。

### A06：SQL Repair 生成 Generation Experience

同一 ApprovedPlan 下首轮 Gate reject、修复 accept，生成 SQL Generation Candidate。

### A07：证据不足不猜 owner

只有“结果不对”但没有结构化修正证据时，进入 `needs_evidence`，不默认归因 Critic。

### A08：Confirm 不改变运行时

Candidate 确认后不改变 `memory_snapshot_id`，下一轮 Agent 输入中不出现该 Experience。

### A09：Experience 生成 Policy Candidate

人工选择同一 Agent 的 Confirmed Experience 后，生成只修改该 Agent `prompt_fragment` 的 PolicyArtifact Candidate，并保存全部来源 ID。

### A10：跨 Agent 和越权 Patch 被拒绝

混选不同 Agent、写 aliases/few-shot/tools/budget/Gate 或修改两个 Agent 时 fail closed。

### A11：定向回放必须解决来源问题

Candidate 没有消除来源 `problem_code` 时，不能进入正式发布评测。

### A12：发布链保持原治理

只有通过 validation、sealed holdout、Shadow、人工差异审核和 Canary 的 Candidate 才能显式激活。

### A13：版本冻结与回滚

运行中激活新 Policy 不改变当前请求；下一轮固定新版本；回滚后恢复历史版本且来源 Experience 保留。

### A14：Trace 展示有界但证据不被物理删除

单会话超过 50 条后页面仍只展示最近窗口，旧 QueryTrace 和其 Experience 来源仍可查询。

### A15：评测与 Shadow 不污染生产经验

validation、sealed holdout、shadow/candidate lane 不生成生产 Experience，也不增加来源次数。

## 15. 非目标

本 MVP 不包含：

- 新增第六个 Agent；
- 改变十一节点拓扑；
- 动态 Experience 召回；
- 自动修改 Knowledge 或 Join Catalog；
- 自动发布 Question-SQL；
- 自动修改工具权限、预算或确定性 Gate；
- 自动批准或激活 Policy；
- 新向量数据库；
- Redis、Celery 或外部任务系统；
- 完整 Event Sourcing 平台；
- 浏览器端完整发布控制台。

## 16. 完成标准

本 MVP 完成的判断不是“Memory 条数增加”，而是至少跑通并复验下面的黄金路径：

```text
一次真实错误 QueryRun
  → 自动生成有前后证据的 Experience Candidate
  → 人工 Confirm
  → 人工选择并自动生成单 Agent Policy Candidate
  → 来源案例定向验证通过
  → validation + sealed_holdout 发布闸门通过
  → Shadow / Canary 通过
  → 显式激活
  → 新请求固定新 Policy 并消除目标问题
  → 可回滚到旧 Policy
  → 全链路能追溯到原 QueryRun 和人工操作
```

只有这条链路成立，才能称为“Memory 分层沉淀与 Policy 自进化最小闭环”。
