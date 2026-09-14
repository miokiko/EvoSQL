# Text2SQL Phase 4：受控自进化闭环

> Memory 主链已升级为 `ExperienceMemory/v1 → SemanticRule/v1 → Policy Candidate`。默认生产治理仍使用 Shadow/Canary/人工发布；另有仅接纳机器可验证证据、通过 Target Replay 与完整独立评测后自动激活的本地 MVP。当前操作以 [Memory / Policy MVP 运行手册](MEMORY_EVOLUTION_MVP_RUNBOOK.md) 为准。

## 结论

本阶段基于独立的 Text2SQL AgentRuntime 与 BoundedRole。当前 `plan-first-text2sql-v3` 以 11 个固定节点承载以下运行拓扑：

```text
Lead 路由 → Evidence Orchestration
          → Schema Grounding || schema-blind Query Planning
          → deterministic bind → Lead 语义审核/每个 Plan Worker 最多定向返工一次
          → Harness 铸造 ApprovedQueryPlan → SQL Generation
          → Harness validate + plan conformance + EXPLAIN
             └─ 零候选通过时，仅允许 SQL Generation 修复一次并重跑门禁
          → Blind Critic → Lead 选择 Critic 接受的已有候选
          → Harness 最终门禁 + 只读执行
```

五个 Agent Role 是 `text2sql-lead`、`schema-grounding`、`query-planning`、`sql-generation` 和 `text2sql-critic`。“进化”只发生在这五个角色的受限 Policy 中；Semantic Experience 只是有证据的离线来源，不能直接改变运行时。Harness 是非 Agent、非 Skill 的应用执行主体，不参与进化。模型不能改 Agent 拓扑、系统代码、确定性绑定、SQL 安全门、数据库权限、数据集或审批状态。当前没有 Critic reject 后的候选修复；唯一一次 SQL 修复发生在 Blind Critic 之前且仅由“首轮零候选通过门禁”触发。

## Policy 可变面

`PolicyArtifact` 当前写出 `text2sql-policy-v2`。每个候选只能修改一个角色：`text2sql-lead`、`schema-grounding`、`query-planning`、`sql-generation` 或 `text2sql-critic`。其中 Query Planning 负责结果粒度、聚合/去重、NULL、排序与结果形状等逻辑计划；SQL Generation 只负责把已批准的绑定计划翻译成 SQL，并承担 SQL Gate 或 plan-conformance 失败的生成侧修复。允许字段只有：

- `prompt_fragments`：追加的人审指导，不覆盖基础系统 Prompt；
- `field_aliases`：别名必须指向当前 schema snapshot 中真实的 `table.column`；
- `value_aliases`：值别名必须绑定真实列，且只能保存有界标量；
- `few_shot_examples`：SQL 必须先通过同一套只读 AST Gate；
- `tool_selection_policy`：只能从角色原权限中删工具，不能增加权限；
- `budget_parameters`：只允许有上下界的 token、时间和步骤预算。

未知字段、写 SQL、提权工具、密钥/凭据内容、安全绕过指令和跨角色修改会在候选入库前失败。Policy 内容做规范化 JSON 哈希，版本号由内容确定。

历史 `text2sql-policy-v1` 在读取时会把旧策略槽迁移到 Planning/Generation；旧 Memory 也按失败类型迁移到新的单一 owner。这个兼容过程只用于加载既有数据，新 Policy、Memory 和命令行写入必须使用上述五个 canonical Skill 名称。

## Experience 到 Policy 的状态机

```text
production QueryTrace + 可验证修订
            ↓
Experience candidate / needs_evidence
            ↓ 人工确认
      confirmed Experience（仍非 Runtime）
            ↓ 人工选择同一 Agent 来源
prompt_fragment-only Policy candidate
            ↓ Target Replay
validation 48 + sealed_holdout 48
            ↓
Shadow → 人工差异审核 → Canary → 人工激活
```

Experience 只记录“哪里出错、如何修正、证据是什么”，始终 `runtime_eligible=false`。人工确认不会改变 `memory_snapshot_id`；只有由 Experience 编译出的 Policy 通过完整发布链并被显式激活，后续请求行为才会改变。`validation`、`sealed_holdout`、Shadow 和 Candidate lane 都不能反向生成生产 Experience。

受限自动链不复用普通 Prompt 候选：它只允许 `ExperiencePolicyProposal/v1` 且必须带完整 `SemanticRulePolicyCompilation/v1` 来源，随后强制执行来源 Target Replay、Validation/Holdout 非退化与安全门禁，并在激活前再次核对 Policy parent、数据库/Vanna/Memory pins、模型、Runtime 与 Principal。任一条件不满足均保持旧 Policy。

查看并审核 Experience：

```bash
python scripts/manage_text2sql_evolution.py memory-list --state candidate
python scripts/manage_text2sql_evolution.py memory-review \
  --memory-id memory-... --decision confirm \
  --actor reviewer-name --human-reviewed
```

从一条或多条同 Agent 的 Confirmed Experience 生成候选：

```bash
python scripts/manage_text2sql_evolution.py auto-propose \
  --memory-id memory-... \
  --actor author-name \
  --reason "修复重复计数与结果粒度规划"
```

这一步只能替换目标 Agent 的完整 `prompt_fragment`，不能修改 aliases、Question-SQL、工具权限、预算或 Gate。候选必须先执行 `scripts/run_text2sql_target_replay.py --candidate policy-...`，之后才可进入独立发布评测。

## Policy 生命周期

初始化控制面：

```bash
python scripts/bootstrap_text2sql_evolution.py
```

导出 active Policy，编辑一个角色后提交候选：

```bash
python scripts/manage_text2sql_evolution.py export-policy \
  --output /tmp/text2sql-policy-candidate.json

python scripts/manage_text2sql_evolution.py propose \
  --artifact /tmp/text2sql-policy-candidate.json \
  --skill query-planning \
  --reason "修正重复计数与结果粒度规划" \
  --actor author-name
```

如果候选只修改从 ApprovedQueryPlan 到 SQL 的构造或门禁对齐规则，则同一命令应使用 `--skill sql-generation`；不能在一个候选里同时修改 Planning 与 Generation。

分别固定 parent/candidate Policy 跑 validation 和 sealed holdout。评测命令从演化库加载完整 Policy 与兼容保留的 Runtime Memory snapshot；新 Confirmed Experience 不在该 snapshot 中。Gold SQL 从不进入 Agent 输入：

```bash
python scripts/run_text2sql_evaluation.py \
  --split validation --split sealed_holdout --max-cases 0 \
  --policy-version policy-... \
  --output artifacts/text2sql/evaluation/candidate.json
```

把同一数据集、同一数据库 snapshot 下的 baseline/candidate 报告交给闸门：

```bash
python scripts/manage_text2sql_evolution.py record-evaluation \
  --candidate policy-... \
  --dataset-manifest evaluation/datasets/text2sql_v1/manifest.json \
  --baseline-report artifacts/text2sql/evaluation/baseline.json \
  --candidate-report artifacts/text2sql/evaluation/candidate.json
```

## 晋升闸门

候选进入 `shadow_ready` 必须同时满足：

- 数据集已人工复核，`review_status=human_reviewed`、`human_reviewed_cases` 覆盖全部 240 题、`release_eligible=true`，且 Phase 6 逐题签名证书验证通过；
- validation EX 至少提升 0.02，净修复题数至少 5；
- validation 和 sealed holdout 的只读安全率都是 100%；
- sealed holdout EX 不下降；
- candidate 每个 split 的 executable rate / AST parse rate 必须非零，且相对 baseline 下降不超过 0.01；
- 任何共享 SQL Skeleton bucket 下降不超过 0.03；
- P95 延迟增长不超过 20%；
- framework error 为 0。

holdout 只有否决权，不能弥补 validation 不达标。门禁不信任报告中的聚合数字：baseline / candidate 必须提供完整、唯一、严格类型化且字段语义一致的匿名逐题 outcome，门禁据此重算 EX、安全率、可执行率、AST 解析率、framework error、P95 延迟和 SQL Skeleton 分桶，并拒绝任一聚合不一致；sealed holdout 任一原本正确的 Case 回退也会直接否决。演化库只保存门禁决策与聚合指标，不保存 holdout 的问题、SQL 或逐题结果。

当前 240 题数据集已完成全量人工复核，Phase 6 签名证书验证通过，`release_eligible=true`。这只解除数据集来源闸门；候选仍须满足本章全部离线指标并通过 Phase 5 shadow/canary 与人工批准。

## 人工上线与回滚

通过全部离线机器闸门后仍不能直接上线。候选先进入 Phase 5 的 shadow、人工差异审核和 canary；只有状态达到 `canary_passed` 后才能显式人工批准：

```bash
python scripts/manage_text2sql_evolution.py approve \
  --candidate policy-... --actor reviewer-name \
  --reason "离线评测、Shadow 与 Canary 均已复核" --human-approved
```

Shadow/Canary 操作见 `PHASE5_SHADOW_RELEASE.md`。

回滚只能选择历史上批准过的 Policy：

```bash
python scripts/manage_text2sql_evolution.py rollback \
  --target policy-... --actor reviewer-name --reason "线上回退"
```

批准和回滚都会写不可省略的 actor、reason、前后版本与时间审计记录。模型没有调用批准接口的运行时工具。

## 数据与实现位置

- 控制面：`artifacts/text2sql/evolution/evolution.sqlite3`（本地状态，不提交）；
- Policy 契约：`evoagent/text2sql/policy.py`；
- Root-cause 候选生成：`evoagent/text2sql/policy_generator.py`；
- 版本、Memory、闸门：`evoagent/text2sql/evolution.py`；
- Multi-Agent 接入：`evoagent/text2sql/agentic.py`；
- 管理入口：`scripts/manage_text2sql_evolution.py`；
- 安全与闭环测试：`tests/test_text2sql_evolution.py`。
