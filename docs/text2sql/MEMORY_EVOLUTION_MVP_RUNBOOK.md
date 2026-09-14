# Memory 分层与 Policy 自进化 MVP 运行手册

适用版本：`text2sql-agentic-build-v18` / `plan-first-text2sql-v3`

## 1. 运行边界

新链路只把可验证的修订沉淀为 `ExperienceMemory/v1`。Experience 即使人工确认，也不会进入 Agent Prompt，也不会改变 `memory_snapshot_id`。线上行为只能通过新的 `PolicyArtifact/v2` 生效。

默认使用下述人工治理链。若明确启用本地自动演化 MVP，则仅接纳 `deterministic_plan_revision`、`deterministic_repair` 或带只读 Gate 证明的用户纠错；纯文字反馈和 `needs_evidence` 会失败关闭。自动模式仍不能修改 Harness、工具 ACL、数据库权限或评测集。

```text
QueryTrace
  → Experience candidate / needs_evidence
  → 人工 confirm
  → 同一 Agent 的 prompt_fragment Policy Candidate
  → Target Replay
  → validation + sealed_holdout（96 条）
  → Shadow → 人工差异审核 → Canary
  → 人工激活
```

受限自动链：

```text
machine-verifiable Experience
  → 自动证据准入 → LLM SemanticRule → 单 Agent Prompt Candidate
  → Target Replay → validation + sealed_holdout
  → 身份/版本再次校验 → 原子激活；失败则保持原 Policy
```

生成待评测候选：

```bash
python scripts/manage_text2sql_evolution.py auto-prepare-pending --limit 10
```

如果控制库仍以 `text2sql-policy-v1` 为当前 Policy，自动准备会先执行一次行为保持迁移：旧 Policy 保留原哈希并进入 retired，新 `text2sql-policy-v2` 取得新的内容哈希和 active pin。迁移不改变任何 Agent Prompt、工具权限或预算，并记录独立 activation audit；它不是一次能力进化。

完成 Target Replay 和两份独立评测后，记录并自动激活：

```bash
python scripts/manage_text2sql_evolution.py \
  --review-key-file /absolute/private/path/text2sql-review.key \
  record-evaluation --candidate policy-... \
  --dataset-manifest evaluation/datasets/text2sql_v1/manifest.json \
  --baseline-report artifacts/text2sql/evaluation/baseline.json \
  --candidate-report artifacts/text2sql/evaluation/candidate.json \
  --auto-activate
```

`--auto-activate` 只接受完整独立评测生成的 `shadow_ready` 候选，并重新检查当前 Policy parent、Database/Vanna/Memory pins、模型与运行时身份、SemanticRule/Experience lineage 及 Target Replay；任一漂移都不会切换版本。生产发布仍建议使用后文的 Shadow/Canary 链。

完整数据集仍是 240 条（train 144、validation 48、sealed_holdout 48）；独立 Policy 发布闸门使用后两组共 96 条。Target Replay 使用来源 QueryTrace，不替代这 96 条独立评测。

## 2. 查询与自动沉淀

Web 查询会自动执行统一 `finalize_run()`。CLI 入口也会写入同一控制库：

```bash
python scripts/run_text2sql.py "按项目统计强烈岩爆案例数" \
  --task-id query-demo-001 \
  --session-id demo
```

响应中的关键字段：

- `memory_status=recorded`：QueryTrace 已保存；
- `memory_status=degraded`：旁路存储失败，但已有 SQL/答案不受影响；
- `experience_ids`：本轮确定性识别出的经验候选；
- `experience_skipped_reason`：普通成功、非生产来源等不生成经验的原因。

只有 Web/CLI 的 stable lane 能自动产生 Experience。evaluation、debug、shadow 和 candidate/canary lane 只记录 QueryTrace，不产生生产经验。

## 3. 查看与审核 Experience

```bash
python scripts/manage_text2sql_evolution.py memory-list --state candidate
python scripts/manage_text2sql_evolution.py memory-list --state needs_evidence
python scripts/manage_text2sql_evolution.py memory-list --state confirmed
```

确认证据、owner Agent 和修正均成立后：

```bash
python scripts/manage_text2sql_evolution.py memory-review \
  --memory-id memory-... \
  --decision confirm \
  --actor reviewer-name \
  --human-reviewed
```

拒绝或标记待补证必须写原因：

```bash
python scripts/manage_text2sql_evolution.py memory-review \
  --memory-id memory-... \
  --decision needs_evidence \
  --review-note "缺少修订后问题消失的结构化证据" \
  --actor reviewer-name \
  --human-reviewed
```

Memory 页面提供相同操作。`needs_evidence` 是当前不可变 revision 的终态；补充证据后由统一提取链生成更高 `source_revision` 的新记录，不能覆盖原始 Evidence。

## 4. 由 Confirmed Experience 生成 Policy Candidate

选择属于同一个 `target_agent` 的一条或多条 Experience：

```bash
python scripts/manage_text2sql_evolution.py auto-propose \
  --memory-id memory-... \
  --memory-id memory-... \
  --actor author-name \
  --reason "修复案例计数的逻辑粒度规划"
```

也可在 Memory 页面多选后点击“生成 Policy 候选”。生成器只允许替换目标 Agent 的完整 `prompt_fragment`；aliases、Question-SQL、工具权限、预算和确定性 Gate 都不能由这条链修改。

输出状态仍是 `candidate`，不会自动上线。Policy 来源会保留全部 `memory_ids` 与字段绑定。

## 5. 来源案例 Target Replay

在独立发布评测前，先对每个来源 QueryTrace 做 parent/candidate 双跑：

```bash
python scripts/run_text2sql_target_replay.py \
  --candidate policy-...
```

默认从 Policy proposal metadata 读取全部来源 Experience。回放要求：

- parent 与 candidate 使用相同 Database、Vanna、Memory、模型、参数和 principals；
- baseline 必须复现来源问题；
- candidate 必须消除对应 `problem_code`；
- candidate 必须安全执行且不能引入新的确定性问题代码；
- Artifact 必须通过内容哈希、来源集合和 Policy parent 校验。
- 每个来源 Experience 必须同时出现在候选的编译证明与物化 lineage 中，并且只能绑定到同一 Agent 的 `prompt_fragment`。

结果写入 `artifacts/text2sql/evolution/target_replays/`，同时绑定到 Evolution Store。失败或缺少 Target Replay 的 Experience-driven Policy 不能调用正式发布评测门禁。

## 6. 独立发布评测

分别固定 parent 和 candidate，完整运行 validation 与 sealed holdout：

```bash
python scripts/run_text2sql_evaluation.py \
  --split validation --split sealed_holdout --max-cases 0 \
  --policy-version policy-parent-... \
  --output artifacts/text2sql/evaluation/baseline.json

python scripts/run_text2sql_evaluation.py \
  --split validation --split sealed_holdout --max-cases 0 \
  --policy-version policy-candidate-... \
  --output artifacts/text2sql/evaluation/candidate.json
```

记录闸门结果：

```bash
python scripts/manage_text2sql_evolution.py record-evaluation \
  --candidate policy-candidate-... \
  --dataset-manifest evaluation/datasets/text2sql_v1/manifest.json \
  --baseline-report artifacts/text2sql/evaluation/baseline.json \
  --candidate-report artifacts/text2sql/evaluation/candidate.json
```

只有 Target Replay 通过、96 条独立评测满足原有指标、运行身份完全一致时，候选才进入 `shadow_ready`。

## 7. Shadow、Canary、激活和回滚

后续治理链没有改变，继续按 [PHASE5_SHADOW_RELEASE.md](PHASE5_SHADOW_RELEASE.md) 操作：

```text
shadow_ready → shadow → 人工差异审核 → canary → canary_passed
                                                   ↓
                                      显式人工 activate

任一已批准历史 Policy ← 显式 rollback
```

激活时会再次核对 parent、版本 Pin、运行身份和 Target Replay。任一内容漂移都要求重新回放。

## 8. 验收检查

```bash
PYTHONPATH=tests .venv/bin/python -m unittest discover -s tests
node --check web/app.js
node tests/test_framework_ui.cjs
```

验收时至少确保：普通成功不产生 Experience；人工 confirm 不改变运行时 Memory；跨 Agent 选源和越权 patch 被拒绝；超过 50 条 QueryTrace 时页面不得物理删除旧证据；评测与 Shadow 不产生生产 Experience。
