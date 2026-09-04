# Text2SQL Phase 6：数据集人工复核与真实 Baseline

## 范围与当前状态

本阶段不重新生成 240 题，也不让模型代替人工审核。当前采用**全量单人复核**：人工逐题确认中文问题、Gold SQL、结果语义以及 Schema/Join Grounding；任何一题 reject 都会阻止签发。

- 数据集：`text2sql-eval-v1-661d4c396c97a3d4`
- 进度：240/240 已由匿名审核员确认通过，0 待审
- 发布资格：`true`
- 复核库：`artifacts/text2sql/review/text2sql_v1_review.sqlite3`
- 首批 20 题：`artifacts/text2sql/review/review_packet_001.jsonl`

逐题事件已经全部写入复核库并通过哈希链校验；证书已用默认私有密钥签发，manifest 为 `human_reviewed + release_eligible=true`。

审查包可能含 sealed holdout 的 Gold 信息，只能供人工审查，不能进入 Agent Prompt、Wiki、Memory 或 Few-shot。

## 逐题复核

```bash
python scripts/review_text2sql_dataset.py status
python scripts/review_text2sql_dataset.py next --limit 1
python scripts/review_text2sql_dataset.py show --case-id t2sql_...
```

无 Join 的题通过示例：

```bash
python scripts/review_text2sql_dataset.py review \
  --case-id t2sql_... \
  --reviewer reviewer-name \
  --verdict approve \
  --question-sql-match pass \
  --result-semantics pass \
  --schema-grounding pass \
  --join-correctness na \
  --notes "人工核对问题、SQL 与结果一致"
```

Join 题必须将 `--join-correctness` 设为 `pass` 或 `fail`。Reject 至少要有一个 `fail` 并填写 notes。同一题再次审核会追加事件，最新结论决定当前状态，旧结论不会被覆盖。

下一批审查包：

```bash
python scripts/review_text2sql_dataset.py packet \
  --limit 20 \
  --output artifacts/text2sql/review/review_packet_002.jsonl
```

## 人工签发

签名密钥由人工生成并放在仓库外，至少 32 bytes，不能交给 Agent Runtime：

```bash
openssl rand -out /absolute/private/path/text2sql-review.key 32
chmod 600 /absolute/private/path/text2sql-review.key
```

只有 240/240 全部 approve 才能执行：

```bash
python scripts/review_text2sql_dataset.py \
  --key-file /absolute/private/path/text2sql-review.key \
  finalize

python scripts/review_text2sql_dataset.py \
  --key-file /absolute/private/path/text2sql-review.key \
  verify
```

签发会生成 `evaluation/datasets/text2sql_v1/review_certificate.json`，并更新 manifest。Loader 会同时核对证书文件哈希、HMAC、数据集/数据库指纹、240 个 Case 内容哈希、审阅者及四项结论。只修改 `release_eligible=true` 无效。

HMAC 证明证书由密钥持有者签发，不是第三方身份认证，因此密钥保管是信任边界。生产评测和晋升可用 `EVOAGENT_TEXT2SQL_REVIEW_KEY_FILE` 指向密钥文件。

## 可续跑真实 Baseline

先做 5 题小跑：

```bash
python scripts/run_text2sql_evaluation.py \
  --split validation --max-cases 5 \
  --max-total-tokens 100000 --max-llm-calls 100 \
  --output artifacts/text2sql/evaluation/baseline-smoke.json
```

中断后使用相同数据集、Case 集、模型和版本固定恢复，并增加 `--resume`。正式晋升报告必须在一次固定身份的运行中覆盖 validation + sealed holdout 共 96 题：

```bash
python scripts/run_text2sql_evaluation.py \
  --split validation --split sealed_holdout --max-cases 0 \
  --max-total-tokens 1000000 --max-llm-calls 1000 \
  --output artifacts/text2sql/evaluation/baseline-release.json
```

Checkpoint 每题落盘并带哈希链，恢复时不会重跑已完成题。Token、调用次数和费用在 Case 之间检查，最多可能超出一题的实际消耗。费用上限需要同时提供输入、输出 Token 单价。

本阶段不会自动发起付费模型调用。真实 96 题 baseline 前应先确认模型、单价与预算。

## 实现

- `evoagent/text2sql/dataset_review.py`
- `evoagent/text2sql/benchmark.py`
- `evoagent/text2sql/evaluation.py`
- `scripts/review_text2sql_dataset.py`
- `scripts/run_text2sql_evaluation.py`
- `tests/test_text2sql_dataset_review.py`
- `tests/test_text2sql_benchmark.py`
