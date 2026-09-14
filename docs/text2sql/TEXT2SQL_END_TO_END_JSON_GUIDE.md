# Text2SQL 全链路与 JSON 字段（速查版）

适用版本：`plan-first-text2sql-v3` / `text2sql-agentic-build-v18`

> 只列跨节点关键字段。`{}` 表示下文定义的公共结构，模型原始文本和工具调用正文不展开。

## 1. 先分清三个概念

- **节点**：固定步骤，可保存 Checkpoint；当前 11 个。
- **Agent**：调用 LLM 判断；当前 5 个角色。
- **Harness**：确定性程序，负责检索、绑定、校验、执行、存储。

| 节点 | 执行者 | 作用 |
|---|---|---|
| 1 路由 | Lead Agent | 分类、认证上下文、委派 |
| 2 证据编排 | Harness + LLM Linker + Vanna | 正向链接、反向解析、Schema 补充 |
| 3 双计划 | Grounding + Planning Agents | 并行生成物理计划与业务计划 |
| 4 计划绑定 | Harness | 业务槽位绑定真实字段和值 |
| 5 计划审核 | Lead Agent | 批准或指定返工 |
| 6 返工审批 | Plan Agents + Lead + Harness | 最多返工一次，批准计划 |
| 7 SQL 生成 | SQL Generation Agent | 按批准计划生成 SQL |
| 8 候选门禁 | Harness；必要时 SQL Agent | 校验候选，最多修复一次 |
| 9 独立审查 | Critic Agent | 审查机器门禁通过的 SQL |
| 10 最终选择 | Harness 或 Lead Agent | 从 Critic 接受项中选择 |
| 11 执行 | Harness | 最终复验并只读执行 |

```text
问题 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11
                                                    ↓
                     QueryTrace → Experience → Policy 自进化
```

## 2. 公共请求

```json
{
  "question": "按项目统计强烈岩爆案例数",
  "task_id": "text2sql-web-...",
  "conversation_context": {
    "scope": {"user_id": "local-user", "session_id": "default"},
    "recent_query_runs": []
  },
  "version_pins": {
    "database_snapshot_id": "...",
    "wiki_index_version": "...",
    "vanna_index_version": "...",
    "memory_snapshot_id": "...",
    "policy_version": "..."
  },
  "protocol": "plan-first-text2sql-v3"
}
```

- `question`：原问题；`task_id`：本轮唯一 ID 和断点恢复键。
- `scope`：用户/会话边界；`recent_query_runs`：可引用的历史查询摘要。
- `database_snapshot_id`：数据库快照；`wiki_index_version`：兼容语料版本。
- `vanna_index_version`：Vanna 索引；`memory_snapshot_id`：运行时 Memory 快照。
- `policy_version`：当前 Agent Policy；`protocol`：运行协议。

## 3. 11 个 Runtime 节点

### 1）`text2sql-lead-routing`

作用：判断新查询、追问、结果问答或澄清；认证历史 QueryRun；决定是否进入 SQL 链。

```json
{
  "input": {"question": "...", "conversation_context": {}, "version_pins": {}},
  "output": {
    "route": {"type": "DATA_QUERY", "standalone_question": "...", "parent_query_run_id": "", "reason": "..."},
    "effective_question": "...",
    "trusted_query_provenance": {},
    "authenticated_parent_snapshot": {},
    "route_gate_errors": [],
    "clarification": {},
    "delegations": [{"assignment_id": "...", "worker": "schema-grounding", "objective": "...", "required_evidence": []}]
  }
}
```

- `route.type`：`DATA_QUERY/FOLLOW_UP_QUERY/RESULT_QA/CLARIFICATION`。
- `standalone_question`：补全后的独立问题；`parent_query_run_id`：引用的历史任务；`reason`：路由理由。
- `effective_question`：后续使用的问题；`trusted_query_provenance`：可信继承值/Join。
- `authenticated_parent_snapshot`：已认证父结果；`route_gate_errors`：认证错误；`clarification`：追问结构。
- `delegations` 子字段：任务 ID、Worker、目标、证据要求。

### 2）`text2sql-evidence-orchestration`

作用：正向 LLM 链接 + 关键词；Vanna 召回 DDL/文档/确认 SQL 并生成**不执行**的草稿；解析草稿表字段；按遗漏和歧义补检。

```json
{
  "input": {"effective_question": "...", "trusted_query_provenance": {}, "version_pins": {}},
  "output": {
    "draft_link_pack": {
      "contract": "SchemaLinkPack/v3",
      "trust": "mixed_untrusted_candidate_input_to_grounding",
      "forward_linking": {"model": {}, "keyword": {}},
      "draft_output": {},
      "tables": [], "columns": [], "joins": [], "value_links": [],
      "logical_concepts": [], "full_ddl": [],
      "unresolved_columns": [], "ambiguous_columns": [],
      "semantic_completion": {"requested": false, "trigger_terms": [], "added_evidence_ids": []},
      "retrieval_evidence_ids": [], "coverage": {}
    },
    "grounding_pack": {}, "planning_business_pack": {},
    "grounding_retrieval_call": {}, "planning_retrieval_call": {},
    "vanna_context_call": {}, "supplemental_retrieval_call": {}
  }
}
```

- `contract/trust`：包版本及“候选线索、非最终事实”标记。
- `forward_linking.model/keyword`：LLM/关键词正向链接；`draft_output`：Vanna 草稿及错误。
- `tables/columns/joins`：候选表、字段、Join；`value_links`：业务值→存储值候选。
- `logical_concepts`：概念→字段候选；`full_ddl`：候选表完整 DDL。
- `unresolved_columns/ambiguous_columns`：未解析/同名歧义字段。
- `semantic_completion`：是否补检、触发词、新证据；`retrieval_evidence_ids`：证据 ID；`coverage`：覆盖状态。
- `grounding_pack`：物理证据包；`planning_business_pack`：无物理 Schema/SQL 的业务包。
- 四个 `*_call`：初次、Vanna 和补充检索调用记录，供审计。

### 3）`text2sql-plan-workers`

作用：两个 Agent 并行；Grounding 决定真实表字段，Planning 决定业务口径。

```json
{
  "input": {"effective_question": "...", "delegations": [], "draft_link_pack": {}, "grounding_pack": {}, "planning_business_pack": {}, "trusted_query_provenance": {}},
  "output": {
    "initial_worker_results": [],
    "worker_results": [
      {"assignment_id": "...", "worker": "schema-grounding", "status": "completed", "memory_evidence_ids": [], "observed_evidence_ids": [], "retrieval": [], "output": {"schema_plan": {}, "grounding_notes": []}, "error": ""},
      {"assignment_id": "...", "worker": "query-planning", "status": "completed", "memory_evidence_ids": [], "observed_evidence_ids": [], "retrieval": [], "output": {"query_spec": {}, "planning_notes": [], "contract_repaired": false}, "error": ""}
    ],
    "clarification": {}
  }
}
```

- `initial_worker_results`：首次结果，不被返工覆盖；`worker_results`：当前有效结果。
- Worker 公共字段：任务 ID、Agent、状态、声明的 Memory、实际可见证据、检索摘要、输出、错误。
- `schema_plan/query_spec`：物理/业务计划；`*_notes`：说明；`contract_repaired`：QuerySpec 是否做过一次格式修复。
- `clarification`：任一 Worker 要求追问时的结构。

### 4）`text2sql-plan-binding`

作用：用程序把 QuerySpec 每个逻辑槽位绑定到 SchemaPlan 的字段和值。

```json
{
  "input": {"query_spec": {}, "schema_plan": {}, "parent_filter_literals": [], "version_pins": {}},
  "output": {
    "bound_query_plan": {},
    "binding_conflicts": [{"code": "missing_schema_binding", "message": "...", "owner": "schema-grounding", "slot_id": "filter-1", "logical_name": "岩爆等级", "candidates": []}],
    "initial_binding_conflicts": []
  }
}
```

- `bound_query_plan`：完整绑定后的不可变计划；`binding_conflicts`：当前冲突；`initial_binding_conflicts`：首次冲突快照。
- 冲突子字段：机器码、说明、责任 Agent、槽位、逻辑概念、物理候选。
- `parent_filter_literals`：可从已认证父查询继承的过滤值。

### 5）`text2sql-lead-plan-assessment`

作用：Lead 只能批准或指定 Worker 返工，不能直接改计划。

```json
{
  "input": {"effective_question": "...", "worker_results": [], "bound_query_plan": {}, "binding_conflicts": []},
  "output": {
    "lead_assessment": {"approve_plan": false, "critic_objective": "...", "reasoning_summary": "...", "revision_request_contract_errors": []},
    "revision_requests": [{"assignment_id": "...", "worker": "schema-grounding", "guidance": "...", "required_evidence": []}],
    "revision_request_contract_errors": []
  }
}
```

- `approve_plan`：是否批准；`critic_objective`：Critic 重点；`reasoning_summary`：审核摘要。
- `revision_requests` 子字段：原任务 ID、责任 Agent、修订指引、必需证据。
- `revision_request_contract_errors`：返工请求格式或归属错误。

### 6）`text2sql-plan-revisions-approval`

作用：被点名的 Plan Agent 最多返工一次，Harness 重新绑定，Lead 最终批准。

```json
{
  "input": {"worker_results": [], "revision_requests": [], "bound_query_plan": {}, "lead_assessment": {}},
  "output": {
    "worker_results": [], "bound_query_plan": {}, "binding_conflicts": [],
    "revisions_applied": 0,
    "lead_plan_approval": {"approve_plan": true, "reasoning_summary": "..."},
    "approved_query_plan": {}, "plan_approval_errors": [], "clarification": {}
  }
}
```

- `worker_results/bound_query_plan/binding_conflicts`：返工后的 Worker、绑定计划和冲突。
- `revisions_applied`：返工数；`lead_plan_approval`：最终审批。
- `approved_query_plan`：带审批与指纹的最终计划；`plan_approval_errors`：阻断错误；`clarification`：仍需追问。

### 7）`text2sql-sql-generation`

作用：复验最多 3 条 Vanna Question-SQL 样例，再按 ApprovedQueryPlan 生成最多 4 个候选。

```json
{
  "input": {"effective_question": "...", "approved_query_plan": {}, "verified_example_pack": {"contract": "VerifiedExamplePack/v1", "authority": "vanna_confirmed_question_sql", "examples": []}, "version_pins": {}},
  "output": {
    "sql_generation_initial": {"worker": "sql-generation", "status": "completed", "memory_evidence_ids": [], "observed_evidence_ids": [], "output": {"sql_candidates": [], "generation_notes": [], "candidate_contract_errors": [], "verified_example_evidence_ids": []}, "error": ""},
    "sql_generation_result": {}
  }
}
```

- `verified_example_pack`：经 Snapshot、安全和计划范围复验的样例，只参考写法。
- `sql_generation_initial/result`：首次结果/当前结果；节点 8 修复后只更新当前结果。
- `sql_candidates`：候选；`generation_notes`：说明；`candidate_contract_errors`：空、重复、非法候选。
- `verified_example_evidence_ids`：实际使用的样例证据；其余 Worker 字段同节点 3。

### 8）`text2sql-candidate-gates`

作用：依次做 AST 安全、计划一致性和 `EXPLAIN`；零候选通过时允许修复一次。

```json
{
  "input": {"sql_generation_result": {}, "approved_query_plan": {}},
  "output": {
    "sql_generation_result": {}, "accepted_candidates": [],
    "candidate_gate_results": [{"candidate_index": 0, "candidate_id": "...", "accepted": true, "validation": {}, "plan_conformance": {}, "explain": {}, "errors": []}],
    "candidate_gate_rounds": [], "sql_generation_repairs": 0
  }
}
```

- `accepted_candidates`：三个门禁都通过的 SQL；`candidate_gate_results`：逐候选结果。
- 子字段：候选序号/ID、是否通过、AST 安全结果、计划一致性、数据库编译结果、错误码。
- `candidate_gate_rounds`：首轮和可选修复轮；`sql_generation_repairs`：0 或 1。

### 9）`text2sql-critic`

作用：独立审查已过机器门禁的匿名候选，不能生成或执行 SQL。

```json
{
  "input": {"question": "...", "original_question": "...", "approved_query_plan": {}, "candidate_gate_results": [], "candidates": [], "critic_objective": "..."},
  "output": {"critic_result": {"action": "final", "decisions": [{"candidate_index": 0, "accepted": true, "objections": [], "supporting_evidence_ids": []}], "summary": "...", "runtime_error": ""}}
}
```

- `question/original_question`：独立问题/用户原话；`critic_objective`：Lead 指定重点。
- `decisions` 必须覆盖每个候选一次；子字段是序号、是否接受、反对意见、支持证据。
- `summary`：总结；`runtime_error`：运行/契约错误，出现时全部 fail closed。

### 10）`text2sql-lead-final`

作用：一个候选时 Harness 直接选；多个时 Lead 只能从 Critic 接受项中选。

```json
{
  "input": {"accepted_candidates": [], "critic_result": {}, "approved_query_plan": {}},
  "output": {"lead_final": {"action": "final", "final_candidate_index": 0, "selection_method": "deterministic_single_candidate", "resolved_objections": [], "resolution_summary": "..."}}
}
```

- `final_candidate_index`：最终序号，`-1` 为不选；`selection_method`：Harness 单候选或 Lead 多选一。
- `resolved_objections`：已处理意见；`resolution_summary`：选择理由。

### 11）`text2sql-final-gates-execute`

作用：重跑最终安全与计划一致性检查；全部通过才只读执行。

```json
{
  "input": {"lead_final": {}, "accepted_candidates": [], "critic_result": {}, "approved_query_plan": {}, "version_pins": {}},
  "output": {
    "status": "success", "selected_candidate": {},
    "gates": {"accepted": true, "errors": [], "ast": {}, "plan_conformance": {}, "bound_plan_fingerprint": "...", "approved_plan_fingerprint": "..."},
    "execution_result": {"columns": [], "rows": [], "row_count": 0, "truncated": false, "elapsed_ms": 0, "explain_plan": [], "sql_fingerprint": "..."},
    "execution_evidence_id": "..."
  }
}
```

- `status`：`success/rejected/needs_clarification/needs_new_query`；`selected_candidate`：最终 SQLCandidate。
- `gates`：是否放行、错误、最终 AST/计划校验、Bound/Approved Plan 指纹。
- `execution_result`：列、行、行数、是否截断、耗时、数据库执行计划、SQL 指纹。
- `execution_evidence_id`：本次只读执行证据 ID。

## 4. 四个核心契约

### 4.1 `QuerySpec/v1`：业务要查什么

```json
{
  "intent": "aggregate", "subject": "岩爆案例",
  "dimensions": [{"slot_id": "dimension-1", "concept": "项目"}],
  "measures": [{"slot_id": "measure-1", "name": "案例数", "aggregation": "count", "field_concept": "案例标识", "distinct": true, "count_all": false}],
  "filters": [{"slot_id": "filter-1", "field_concept": "岩爆等级", "operator": "eq", "value": "强烈", "scope": "where"}],
  "order_by": [{"slot_id": "order-1", "target": "案例数", "direction": "desc"}],
  "limit": 20, "expected_shape": "grouped_rows", "distinct_rows": false, "version": 1
}
```

- `intent/subject`：意图与主体；`dimensions`：维度槽位/业务概念。
- `measures`：指标槽位、名称、聚合、业务字段、聚合去重、是否 `COUNT(*)`。
- `filters`：WHERE 槽位、字段、操作符、值、作用域；`order_by`：排序槽位、目标、方向。
- `limit`：行数上限；`expected_shape`：`scalar/rows/grouped_rows`；`distinct_rows`：整行去重；`version`：版本。

### 4.2 `SchemaPlan/v1`：真实表字段

```json
{
  "tables": ["t_caseinfo"], "columns": ["t_caseinfo.c_caseCode"],
  "joins": [{"left": "t_a.c_id", "right": "t_b.c_aId", "join_type": "inner", "evidence_id": "...", "source": "stable"}],
  "result_grain": ["t_caseinfo.c_projectId"], "evidence_ids": [],
  "bindings": [{"logical_name": "案例标识", "column": "t_caseinfo.c_caseCode", "aliases": ["案例编码"], "evidence_ids": [], "value_bindings": [{"logical_value": "强烈", "physical_value": "强烈岩爆", "evidence_ids": []}]}]
}
```

- `tables/columns`：允许使用的表和 `表.字段`；`result_grain`：每行结果粒度。
- `joins`：左右字段、Join 类型、证据、来源；来源是 `stable/user_explicit/draft_inferred`。
- `evidence_ids`：计划证据；`bindings`：逻辑名、物理字段、同义词、证据。
- `value_bindings`：用户业务值→数据库存储值及其证据。

### 4.3 `ApprovedQueryPlan/v1`：SQL 唯一语义边界

```json
{
  "contract": "ApprovedQueryPlan/v1",
  "bound_plan": {
    "contract": "BoundQueryPlan/v1", "query_spec": {}, "schema_plan": {},
    "bindings": [{"slot_id": "filter-1", "kind": "filter", "logical_name": "岩爆等级", "column": "t_caseinfo.c_level", "aggregation": "", "distinct": null, "operator": "eq", "value": "强烈岩爆", "logical_value": "强烈", "direction": "", "scope": "where", "evidence_ids": []}],
    "version_pins": {}, "fingerprint": "..."
  },
  "approved_by": "text2sql-lead", "approval_reason": "...", "approval_id": "...", "fingerprint": "..."
}
```

- `query_spec/schema_plan`：业务/物理计划；`bindings`：每个逻辑槽位最终实现。
- Binding 子字段依次为槽位、类型、逻辑名、字段、聚合、去重、操作符、物理值、原业务值、排序、过滤作用域、证据。
- `version_pins`：固定版本；两个 `fingerprint`：Bound/Approved Plan 内容指纹。
- `approved_by/reason/id`：审批人（固定 Lead）、理由、审批 ID。

### 4.4 `SQLCandidate`

```json
{
  "candidate_id": "harness-r0-c1-...", "sql": "SELECT ...", "query_spec_version": 1,
  "database_snapshot_id": "...", "wiki_index_version": "...", "vanna_index_version": "...", "memory_snapshot_id": "...", "policy_version": "...",
  "revision": 0, "evidence_ids": [], "bound_plan_fingerprint": "..."
}
```

- `candidate_id/sql`：Harness 候选 ID/SQL；`query_spec_version`：业务计划版本。
- 五个版本字段：数据库、兼容语料、Vanna、Memory、Policy 固定值。
- `revision`：0 首次、1 门禁修复；`evidence_ids`：依据；`bound_plan_fingerprint`：必须实现的计划。

## 5. 查询后的 Memory / Policy 链

这部分不是 11 个 SQL 节点，而是查询结束后的旁路和发布控制面。

| 阶段 | 输入 | 输出 | 方式 |
|---|---|---|---|
| 保存 Trace | 节点 11 结果 + 内部状态 | QueryTrace | 自动 |
| 提炼经验 | Trace 中的真实修订证据 | Experience candidate | 自动 |
| 审核经验 | Experience + 证据 | confirmed/rejected/needs_evidence | 人工门禁 |
| 生成 Policy | 同一 Agent 的 confirmed Experiences | Policy candidate | 人工触发、系统生成 |
| Target Replay | 来源 Trace + parent/candidate | 回放报告 | 系统执行 |
| 发布评审 | validation 48 + sealed 48 | shadow_ready/失败 | 系统执行 |
| 发布 | Shadow → Canary | active Policy | 人工审核/激活 |

### 5.1 Trace/Experience 写入状态

```json
{
  "memory_status": "recorded", "trace_recorded": true, "task_id": "...",
  "origin": "web", "source_lane": "stable",
  "experience_count": 0, "experience_ids": [], "experience_states": [],
  "experience_skipped_reason": "", "error": ""
}
```

- `memory_status/trace_recorded`：旁路状态/Trace 是否落库；`task_id/origin/source_lane`：任务、入口、通道。
- `experience_count/ids/states`：提炼经验；`experience_skipped_reason`：未生成原因；`error`：旁路错误。
- 只有 `web/cli + stable + DATA_QUERY` 可能沉淀；普通成功不会硬造经验。

### 5.2 `ExperienceMemory/v1`

```json
{
  "contract": "ExperienceMemory/v1", "memory_id": "memory-...",
  "source_task_id": "...", "source_revision": 1,
  "target_agent": "query-planning", "source_stage": "plan-revisions", "problem_code": "query_planning_plan_revision",
  "scenario": "...", "problem": "...", "correction": "...", "applicability": {},
  "before": {}, "after": {}, "evidence": {}, "evidence_grade": "deterministic_repair",
  "state": "candidate"
}
```

- `memory_id`：经验 ID；`source_task_id/revision`：来源 Trace 和不可变版本。
- `target_agent/source_stage/problem_code`：改谁、问题在哪、机器问题码。
- `scenario/problem/correction/applicability`：场景、问题、修法、适用边界。
- `before/after/evidence/evidence_grade`：修订前后、证明、证据等级。
- `state`：`candidate/needs_evidence/confirmed/rejected`；Confirmed 也不直接进 Prompt。

### 5.3 Policy Candidate 与验证

```json
{
  "policy_candidate": {
    "parent_policy_version": "...", "policy_version": "...", "target_agent": "query-planning",
    "skill_patch": {"prompt_fragment": "..."},
    "memory_ids": ["memory-..."], "memory_field_bindings": {"memory-...": ["prompt_fragment"]},
    "rationale": "...", "artifact": {}
  },
  "target_replay": {
    "contract": "TargetReplayArtifact/v1", "status": "passed",
    "summary": {"source_experience_count": 1, "passed_count": 1, "failed_count": 0},
    "results": [], "artifact_sha256": "..."
  },
  "release_evaluation": {"validation_cases": 48, "sealed_holdout_cases": 48, "status": "passed"}
}
```

- `parent_policy_version/policy_version/target_agent`：父版本、候选版本、目标 Agent。
- `skill_patch.prompt_fragment`：唯一可改字段；`memory_ids`：全部来源经验；`memory_field_bindings`：经验影响字段。
- `rationale/artifact`：修改理由/完整候选 Policy。
- `target_replay`：来源问题是否复现并修好；`summary/results`：汇总/逐经验结果；`artifact_sha256`：报告指纹。
- `release_evaluation`：48 条 validation + 48 条 sealed holdout 的独立发布门禁。

```text
candidate → Target Replay 通过 → 96 条评审通过
→ shadow_ready → shadow → 人工差异审核 → canary
→ canary_passed → 人工 activate
```

## 6. 特殊分支

| 路由 | 行为 |
|---|---|
| `CLARIFICATION` | 跳过 SQL 规划/生成，节点 11 返回追问 |
| `RESULT_QA` | 只安全重显已认证结果，不生成新 SQL |
| `FOLLOW_UP_QUERY` | 认证父 QueryRun，重写独立问题后走完整链路 |

一句话记忆：**节点 2 找候选证据，节点 3 分别做业务规划和物理落地，节点 4/6 锁成 ApprovedQueryPlan，节点 7 以后只忠实翻译和验证；Experience 保存教训，Policy 才改变 Agent。**
