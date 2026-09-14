# EvoSQL 简历声明与闭环审计

审计日期：2026-09-07  
审计基线：`plan-first-text2sql-v3` / `text2sql-agentic-build-v18` / `text2sql-harness-gates-v10`

## 结论

项目已经具备完整的工程闭环接口：查询运行、Trace、Experience 提取、规则归纳、单 Agent Prompt 候选、Target Replay、独立评测、自动激活与回滚均有代码与失败关闭约束。一条真实 Query Planning Experience 已完成规则归纳、候选 Prompt 和 Target Replay，并因没有复现基线问题、候选引入 Critic 拒绝而正确停止，未进入独立评测或激活。这证明失败关闭链有效，但尚无“候选通过并提升”的真实发布记录。v3 全量评测已覆盖 240 题，但有 1 题因模型服务连接中断被标为基础设施错误。因此“具备受控自进化能力”可以写，“已经自动进化并取得提升”暂不能写。

## 逐条核对

| 简历声明 | 状态 | 代码事实 | 需要修正的表达 |
|---|---|---|---|
| Leader–Workers–Critic Runtime | 已实现 | 5 个 Agent Role、11 个 Runtime Node；Schema Grounding 与 Query Planning 在 plan-workers 节点并行；Critic 盲审；Harness 独占执行权限 | 项目内正式名称是 `Lead`；SQL Generation 是后置 Agent，不属于并行的两个 Plan Workers |
| Tool ACL、Run Trace、Checkpoint | 已实现 | 角色/阶段 ACL 取交集；Query Planning、SQL Generation、Critic 无工具；QueryTrace 与调用账本持久化；Checkpoint 绑定完整运行身份并支持恢复 | “断点续跑”成立，但仅在所有版本 Pin、模型、预算、节点图一致时恢复 |
| Vanna + 四路 Schema Linking | 机制已实现 | Vanna 只负责 DDL、Documentation、Confirmed Question-SQL 检索；正向 LLM、关键词、草稿 SQL AST 回溯、缺口 Schema 补充形成四类候选通道 | 四路不是四个 Vanna Retriever，宜写“四路候选发现/补全机制” |
| 字段 Recall 83%→94%、F1 87%→92% | 未证实 | 当前评测器记录 table/column recall，没有 schema-link precision/F1 的前后消融报告 | 在补齐固定数据集、baseline 定义、预测集合与消融报告前删除这组数字 |
| SchemaPlan / QuerySpec / ApprovedQueryPlan | 已实现 | 两个 Plan 经 deterministic Binder 绑定；Lead 给出审批判断；ApprovedQueryPlan 由 Harness 铸造并做 fingerprint 固化 | 不应写成“Lead 自己固化计划”，应写“经 Lead 审批，由 Harness 固化” |
| Join/Fanout 与 SQL 门禁 | 已实现 | Join Catalog、基数/fanout 风险、SQLGlot AST、表列白名单、计划一致性、EXPLAIN、只读 SQLite、超时和最大行数均存在 | 当前 SQL 表达能力是受限子集，不支持任意 CTE、子查询、自连接和复合 Join |
| Working / Episodic / Semantic Memory | 已实现 | Working 保存会话；Episodic 保存 QueryRun/反馈/版本；Semantic Experience 保存可回放的前后差异证据 | Semantic Experience 不在运行时“定向召回”；其正确去向是离线 SemanticRule 与对应 Agent Policy |
| Confirmed Question-SQL 回流 Vanna | 已实现 | 正反馈经来源检查与当前 SQL Gate 复验后进入 Vanna Question-SQL；与错误学习链隔离 | “用户确认”不能单独作为入库依据，仍需 SQL 复验 |
| 自动归因、规则提炼、候选 Prompt | 已实现自动 MVP | 机器准入仅支持确定性 Plan 修订、确定性 SQL 修复、带 Gate 证明的用户纠错；LLM 只归纳 SemanticRule 和 Prompt | 普通 Trace 错误不会全部进入 Bad Case；无法唯一归因或只有文字说明的异常会失败关闭 |
| Target Replay + Validation/Holdout + 自动激活 | 代码已实现，真实失败关闭已验证 | 自动通道只接受 `Experience → SemanticRule → 单 Agent Prompt` 来源；Target Replay、96 条独立评测、身份与版本复核全部通过后才原子切换；支持回滚 | 当前演化库为 1 条 confirmed SemanticRule、1 次 failed Target Replay、0 次 evolution run；候选未激活，不能声称已有真实提升结果 |
| 当前 v3 全量评测 | 未形成完整有效报告 | 已覆盖 240 题；239 题为有效模型结果，其中 212 题执行结果正确，有效样本 EX 为 88.70%；1 题因阿里云连接中断为 `FRAMEWORK_ERROR`，报告状态为 `incomplete_infrastructure_error` | 不能把 88.70% 写成无条件的 240 题正式成绩，也不能继续使用旧 8 节点 93.75% 代表当前架构 |

## 当前闭环

```text
用户查询
  → 11 节点 Text2SQL Runtime
  → QueryTrace + QueryRun + 用户反馈
  → 确定性 Experience 提取
      ├─ 证据不完整：needs_evidence / 停止
      └─ 前后证据可验证：自动准入
          → LLM 提炼同 Agent SemanticRule
          → 编译 prompt_fragment-only Policy Candidate
          → 来源 QueryTrace Target Replay
          → Validation 48 + Sealed Holdout 48
          → SQL 执行安全、指标非退化、身份/版本/来源复核
              ├─ 任一失败：旧 Policy 保持激活
              └─ 全部通过：原子激活新 Policy
                  → 后续请求固定新版本
                  → 异常时回滚历史已批准 Policy
```

自动链不能修改 Harness、SQL Gate、工具权限、预算、数据库权限、知识事实、Join Catalog 或评测集；普通人工 Prompt 候选也不能借自动通道发布。

## 仍需补齐的真实证据

### P0：简历真实性与面试必问

1. 重跑并补齐剩余 1 条阿里云基础设施错误，使当前 v3 的 240 题报告达到 `complete`；中断 checkpoint 和旧 8 节点 93.75% 报告不能作为当前架构成绩。
2. 当前真实 Experience 已在 Target Replay 失败关闭：基线未稳定复现原 `unsupported_query_contract`，候选新增 `critic_rejected_all_candidates`。该问题实际需要表达“按项目分组聚合后再取最大值”的嵌套聚合，而 `QuerySpec/v1` 无法完整表达；应扩展计划契约，或选择真正可由 Prompt 修复的 Bad Case，再完成一次通过 Target Replay 和 96 条发布评测的成功记录。
3. 为 Schema Linking 增加固定 Gold 表列集合、Precision/Recall/F1 与四路消融；在报告生成前删除 83→94、87→92。
4. 使用不暴露物理表列的真实业务问法补一组评测。现有 240 题来自数据库快照确定性生成，很多问题直接给出表名和字段名，不能充分证明自然语言 Schema Linking 能力。

### P1：质量与成本

1. 优先处理 12 条 `CRITIC_REJECTION`。当前最大错误源不是 SQL 执行器，而是 Critic 对 Top-K、标量聚合等可执行候选的过度拒绝；应把可机器判定的规则下沉到 Harness，并校准 Critic 的拒绝边界。
2. 修正 6 条 `JOIN_OR_GRAIN_MISMATCH`，统一 `SchemaPlan.result_grain`、`QuerySpec.group_by` 与 Binder 的分组粒度语义，避免把合法分组查询误判为粒度冲突。
3. 对“过滤列与投影列相同”、空字符串值等合法边界进行槽位去重和确定性表示，降低伪歧义、值缺失及候选生成失败。
4. 用修正后的评测器重跑 Join 指标。旧报告只按 `evidence_id` 计分，而运行时会按安全设计清除用户显式 Join 的模型来源 ID，导致成功 Join 仍为 0；现已改为按规范化物理端点计分，但旧报告不能回填新指标。
5. 分析 Validation 89.58% 与 Sealed Holdout 79.17% 的 10.41 个百分点落差，按查询骨架与错误类型检查数据分布、Prompt 过拟合及规则泛化，而不是只看总体 EX。
6. 降低默认链路约 8 次模型调用及当前分钟级尾延迟；缓存确定性证据包，并只在有歧义时触发草稿/补充召回。当前报告 p50 为 74.1 秒、p95 为 94.3 秒，1956 次模型调用、约 1069.8 万 Token。
7. 配置实际模型输入/输出单价和请求级预算；当前报告中的 cost 为 0 只表示价格未配置。
8. 增加澄清分支的真实模型 E2E 回归，覆盖 Lead 非法 action、补充信息重提问和幂等重试。
9. 逐步扩展 ApprovedQueryPlan 与 conformance 对 CTE、受限子查询、复合 Join、自连接的表达能力。

### P2：生产化

1. 补 CI、可追溯 Git 历史、依赖锁定和构建产物证明；当前工作副本不是 Git 仓库，无法从本地历史证明个人贡献。
2. 增加租户级知识 ACL、行列数据权限与审计保留策略；Tool ACL 不能替代数据权限。
3. 对接生产数据库只读副本并补资源隔离、并发压测、SLO、告警和灾难恢复；当前主执行后端是本地 SQLite。
4. 区分实验室原始研发时间与 2026 年重构时间。简历写 2024.09–2025.12，而当前 v18 工件生成于 2026-09，需要准备可核验的版本演进说明。

## 推荐的简历事实边界

- 可以写：实现、设计、构建、支持、具备、门禁约束。
- 暂缓写：字段 Recall/F1 的具体增益、自动进化后的效果提升、生产级无人发布。
- 自进化建议写为：自动生成并验证候选 Policy；只有在真实演化运行与完整报告留档后，再写“自动激活新版本并取得提升”。

## 验证记录

- 自动演化新增测试：6/6 通过。
- Memory、Policy、SemanticRule、Target Replay、Checkpoint 等定向 Python 回归：123 项通过。
- Benchmark 与评测基础设施回归：13 项通过；模型服务错误现在会独立归类为 `FRAMEWORK_ERROR`，不会污染 Agent 失败指标或把不完整报告标为完成；Join Edge 指标已覆盖用户显式 Join 的端点计分。
- 前端语法及澄清/Memory UI 测试：通过。
- 全量 Python 测试运行 367 项，其中 358 项通过，9 项 API 测试因当前沙箱禁止绑定本地端口而在 `setUp` 报错；这不是 API 通过记录，需在允许监听端口的环境重跑。

## 当前 v3 全量评测快照

- 报告状态：`incomplete_infrastructure_error`。
- 覆盖：240 / 240；有效结果 239，基础设施错误 1。
- 有效结果：212 / 239 正确，条件 EX 为 88.70%；若把基础设施错误计入全部样本，则为 88.33%。
- 分组表现：Validation 89.58%，Sealed Holdout 79.17%；Holdout 明显更低，需要单独排查泛化问题。
- 只读安全率：100%；表、列召回均为 99.58%；值对齐准确率为 80.45%。旧报告的 Join Edge Recall 因历史评测器只认来源 ID 而失真，必须重跑后再引用。
- 延迟与消耗：p50 74.1 秒，p95 94.3 秒，1956 次模型调用，约 1069.8 万 Token；费用字段未配置，不能据此声称成本为 0。
- 失败主因：Critic 拒绝 12 条、Join/粒度不一致 6 条、Schema Linking 不一致 3 条，其余为候选生成、聚合、过滤、规划和值对齐问题。
- 唯一基础设施阻塞：`t2sql_d34cae45f4c014043769`，阿里云连接多次被远端关闭。
- 证据：`artifacts/text2sql/evaluation/full-240-qwen-plus-v3-final2-20260907.json`。

## 首次真实自进化运行

- 来源 Experience：`memory-59bb47e1c26eafd2a7d15cf1`，归因到 `query-planning`。
- 生成 SemanticRule：`semantic-rule-c63286ef4fbb8f1f6f05aedd`。
- 候选 Policy：`policy-d2e48c6547c11d278246`；Parent 为行为等价迁移后的 v2 Policy `policy-2999731bf0c42805e0a1`。
- Target Replay：失败。基线没有稳定复现来源问题；候选新增 `critic_rejected_all_candidates` 且没有安全可执行结果。
- 发布结果：未运行 Validation/Holdout，未激活候选，当前 Policy 保持不变。
- 工程结论：门禁正确阻止了一条“把无法表达的嵌套聚合压成普通标量聚合”的危险 Prompt。该 Bad Case 属于计划契约能力缺口，不能靠 Prompt 强行进化。
- 证据：`artifacts/text2sql/evolution/target_replays/policy-d2e48c6547c11d278246.json`。
