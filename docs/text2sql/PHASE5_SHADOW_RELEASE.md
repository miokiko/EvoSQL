# Text2SQL Phase 5：Shadow 与 Canary 发布

## 发布链路

Phase 5 沿用原 EvoAgent 的确定性 rollout 思路，但差异判断改成 Text2SQL 语义，并且取消自动上线：

```text
offline shadow_ready
        ↓ 5% 确定性 shadow
stable 与 candidate 隔离双跑，始终返回 stable
        ↓ 样本/错误率/结果一致性/P95 闸门
shadow_review
        ↓ 人工审核全部差异
canary_ready
        ↓ 5% canary；失败立即返回 stable 并回滚
canary_passed
        ↓ 再次人工批准
active stable Policy
```

没有 candidate 时只运行 active stable，不增加模型调用。5% shadow 命中时，本地 CLI 会并发运行两个独立 `Text2SQLAgenticEngine`；两条 lane 都执行 `plan-first-text2sql-v3` 的五 Agent、非 Agent Harness 与 11 个固定节点，candidate 只能读同一只读数据库，并拥有独立 Policy 实例和运行轨迹。shadow candidate 的结果绝不会成为用户响应。

## 配置 Shadow

只有已通过完整 validation + sealed holdout、状态为 `shadow_ready`，且 parent 仍是当前 active Policy 的候选可以进入 shadow：

```bash
python scripts/manage_text2sql_evolution.py shadow-configure \
  --candidate policy-... --actor release-operator \
  --percent 5 --min-samples 20 \
  --max-failure-rate 0 \
  --max-result-disagreement 0.2 \
  --max-p95-multiplier 1.2
```

配置时会再次核对 database snapshot、stable Wiki index 和 stable Memory snapshot 是否与离线评测一致。shadow/canary 运行期间任一版本漂移都会停止部署，保留 stable 响应。

`scripts/run_text2sql.py` 已自动读取 release 配置。抽样使用 `deployment_id + task_id` 的 SHA-256 桶，同一任务稳定落在同一桶：

```bash
python scripts/run_text2sql.py "强烈岩爆案例有多少个" --task-id request-0001
```

## 差异记录

Shadow Store 不保存用户问题、原始 SQL、结果行、Wiki 页面 ID 或候选异常正文，只保存：

- task key SHA-256；
- stable/candidate SQL SHA-256；
- 将 Literal 替换为占位符后的 SQL Skeleton；
- 结果集指纹及是否等价；
- Wiki 引用 ID 的不可逆哈希增删集合；
- stable/candidate 延迟；
- candidate 异常指纹；
- Policy、数据库、Wiki 和 Memory 版本。

因此可以判断“SQL 是否变化、结果是否相同、引用是否漂移、延迟是否超限”，但不会把线上问题或敏感结果写进自进化存储。

## 自动回落与 Shadow Review

Candidate 出现模型异常、安全 Gate 异常，或者在 stable 成功时自身失败，会立即：

1. 返回 stable 结果；
2. 将部署改为 `rolled_back`；
3. 将候选改为 `shadow_rejected`；
4. 写入自动回落审计。

达到最小 shadow 样本数后还会检查 candidate failure rate、结果不一致率和 P95 延迟。通过后停止抽样并进入 `shadow_review`。

```bash
python scripts/manage_text2sql_evolution.py shadow-status --deployment shadow-...
python scripts/manage_text2sql_evolution.py shadow-observations \
  --deployment shadow-... --review-state pending
```

所有 SQL、结果或 Wiki 引用差异都需要人工给出结论：

```bash
python scripts/manage_text2sql_evolution.py shadow-review \
  --observation shadow-observation-... \
  --verdict equivalent \
  --actor reviewer-name \
  --reason "结果等价，SQL 结构变化可接受" \
  --human-reviewed
```

可选结论为 `equivalent`、`candidate_better`、`stable_better`、`reject`。存在后两种结论时不能通过 shadow。

全部差异审核完成后：

```bash
python scripts/manage_text2sql_evolution.py shadow-approve \
  --deployment shadow-... --actor reviewer-name \
  --reason "已完成全部差异审核" --human-approved
```

## Canary 与最终激活

Canary 会把命中的请求交给 candidate，但 stable 仍并行运行作为即时 fallback。任何 candidate 运行或安全失败都会返回 stable 并自动回滚发布：

```bash
python scripts/manage_text2sql_evolution.py canary-start \
  --deployment shadow-... --actor release-operator \
  --percent 5 --min-samples 20
```

达到样本数且未超错误预算后状态变为 `canary_passed`。系统仍不会自动激活，必须再次人工批准：

```bash
python scripts/manage_text2sql_evolution.py approve \
  --candidate policy-... --actor reviewer-name \
  --reason "Shadow 与 Canary 均通过" --human-approved
```

历史 Policy 回滚会同时终止与该候选关联的 shadow/canary deployment：

```bash
python scripts/manage_text2sql_evolution.py rollback \
  --target policy-... --actor reviewer-name --reason "线上回退"
```

## 当前状态

当前演化库只有空 baseline，没有 candidate 和 release deployment。240 题评测集已完成签名人审，但尚未运行付费模型的完整 validation + sealed holdout baseline，因此现在仍没有候选可以进入真实 shadow。

实现位于：

- `evoagent/text2sql/shadow.py`；
- `scripts/run_text2sql.py`；
- `scripts/manage_text2sql_evolution.py`；
- `tests/test_text2sql_shadow.py`。
