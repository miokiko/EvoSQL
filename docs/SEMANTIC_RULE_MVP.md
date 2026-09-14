# Role-scoped SemanticRule MVP

Experience 保存纠错事实；SemanticRule 由离线 LLM 归纳原因、方法、适用条件和例外；
Policy 将同一 Agent 的已确认规则编译为该角色的完整 `prompt_fragment` 候选。
Working/Episodic、在线 Agent、工具权限、数据库安全门禁均沿用原实现。

## 使用流程

1. 正式查询产生可验证的用户纠错、计划修订或 SQL Gate 修复 Experience，人工确认该案例。
2. Memory 页面点击案例的“归纳语义规则”。每次最多一次 LLM 调用；无完整证据或无可复用结论时跳过。
3. 核对目标 Agent、原问题、角色证据、适用条件和例外，确认或拒绝规则。
4. 选择同一 Agent 的已确认规则，点击“编译 Agent 策略候选”。确认规则和生成候选都不会改变运行时。
5. 使用原有 Target Replay、96 题独立评测、Shadow、Canary、人工激活和回滚流程。

## CLI

在项目目录运行：

```bash
.venv/bin/python scripts/manage_text2sql_evolution.py rule-generate --memory-id memory-... --actor author
.venv/bin/python scripts/manage_text2sql_evolution.py rule-list --state candidate
.venv/bin/python scripts/manage_text2sql_evolution.py rule-review --rule-id semantic-rule-... --decision confirm --actor reviewer --human-reviewed
.venv/bin/python scripts/manage_text2sql_evolution.py policy-from-rules --rule-id semantic-rule-... --actor author
.venv/bin/python scripts/run_text2sql_target_replay.py --candidate policy-...
```

拒绝规则时需 `--review-note`；多个规则可重复传入 `--rule-id`，上限 20。
Web 的生成、审核及编译接口均要求 `manage` 权限，审核人与作者使用认证身份并写入审计。

## 证据与发布边界

- 支持五个 Text2SQL Agent 的已确认、可回放 Experience；用户纠错、计划修订与 SQL Gate 修复分别使用对应证据适配器。
- 按 `source_task_id + source_revision` 读取原始轨迹；不能用最新轨迹代替历史版本。
- 计划修订和 SQL 修复会重新提取确定性证明；用户纠错必须匹配人工决策。指纹只验证身份，LLM 同时读取受限的语义证据。
- 模型输入不包含查询结果行、原始 Prompt 或隐藏推理；沿用已有脱敏机制。
- `semantic_rules` 独立存储，状态为 candidate/confirmed/rejected。正文不可变，修改应创建新规则记录并重新审核。
- 规则不进入运行时 Memory 检索，不改变 `memory_snapshot_id`。
- Policy 同时绑定规则内容哈希及全部 Experience id；Target Replay 继续回放原始案例。
- 提案和发布时验证规则确认状态、原始证据及来源集合；不允许丢弃来源或修改其他策略字段。
- 每条规则只归属一个 Agent；同一 Policy 候选禁止混用多个 Agent 的规则。原有 Experience 直接编译入口仅保留兼容。

## 验证

```bash
PYTHONPATH=tests .venv/bin/python -B -m unittest test_text2sql_semantic_rules test_text2sql_api
node --check web/app.js
node tests/test_semantic_rule_ui.cjs
node tests/test_framework_ui.cjs
```

集成测试使用合成纠错案例和临时控制库，覆盖五个 Agent 的规则生成与单角色 Policy 编译。
脚本模型用于验证流程、隔离、来源校验和定向回放接口，不代表真实模型效果提升。
