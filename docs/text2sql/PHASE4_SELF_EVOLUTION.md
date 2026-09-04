# Text2SQL Phase 4：受控自进化闭环

## 结论

本阶段没有替换原 EvoAgent Multi-Agent 框架。运行拓扑仍是：

```text
Lead → Schema Grounding + SQL Strategy → Lead 检查/一次返工 → Blind Critic
     → Lead 选择已有候选 → 确定性 SQL Gate → 只读执行
```

“进化”只发生在四个角色的受限 Policy 和人工审核的 stable Memory 中。模型不能改 Agent 拓扑、系统代码、SQL 安全门、数据库权限、数据集或审批状态。

## Policy 可变面

每个候选只能修改一个角色：`text2sql-lead`、`schema-grounding`、`sql-strategy` 或 `text2sql-critic`。允许字段只有：

- `prompt_fragments`：追加的人审指导，不覆盖基础系统 Prompt；
- `field_aliases`：别名必须指向当前 schema snapshot 中真实的 `table.column`；
- `value_aliases`：值别名必须绑定真实列，且只能保存有界标量；
- `few_shot_examples`：SQL 必须先通过同一套只读 AST Gate；
- `tool_selection_policy`：只能从角色原权限中删工具，不能增加权限；
- `budget_parameters`：只允许有上下界的 token、时间和步骤预算。

未知字段、写 SQL、提权工具、密钥/凭据内容、安全绕过指令和跨角色修改会在候选入库前失败。Policy 内容做规范化 JSON 哈希，版本号由内容确定。

## Memory 状态机

```text
train / production feedback
            ↓
      candidate memory
            ↓ 人工审核
      stable 或 rejected
```

只有 `stable` 可进入角色上下文。`validation` 不写 Memory，`sealed_holdout` 被代码硬拒绝；这样不会一边看考题一边学习。Memory 只是提示，schema snapshot、stable KnowledgeStore、SQL Gate 和只读执行器永远优先。

从 train 评测报告提取失败候选：

```bash
python scripts/manage_text2sql_evolution.py capture-training-failures \
  --report artifacts/text2sql/evaluation/train.json \
  --skill sql-strategy
```

查看、审核：

```bash
python scripts/manage_text2sql_evolution.py memory-list --state candidate
python scripts/manage_text2sql_evolution.py memory-review \
  --memory-id memory-... --decision approve --actor reviewer-name --human-reviewed
```

审核后 `memory_snapshot_id` 自动变化，后续评测会固定这个新版本。

如果已配置原项目支持的 LLM，可以让 EvoAgent 的 root-cause evolution role 对 stable failure Memory 聚类，并自动生成“单角色、白名单字段”的候选：

```bash
python scripts/manage_text2sql_evolution.py auto-propose \
  --skill sql-strategy --actor author-name
```

这一步只生成 `candidate`，不会自动评测、批准或上线；模型输出还会再次经过 `PolicyArtifact` 的确定性校验。

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
  --skill sql-strategy \
  --reason "修正重复计数和 Join fanout 失败" \
  --actor author-name
```

分别固定 parent/candidate Policy 跑 validation 和 sealed holdout。评测命令从演化库加载完整 Policy 与 stable Memory；Gold SQL 从不进入 Agent 输入：

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
- executable rate / AST parse rate 下降不超过 0.01；
- 任何共享 SQL Skeleton bucket 下降不超过 0.03；
- P95 延迟增长不超过 20%；
- framework error 为 0。

holdout 只有否决权，不能弥补 validation 不达标。演化库只保存 holdout 聚合指标，不保存 holdout 的问题、SQL 或逐题结果。

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
