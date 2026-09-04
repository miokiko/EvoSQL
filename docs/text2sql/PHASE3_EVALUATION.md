# Text2SQL Phase 3：独立评测集

## 数据集边界

`text2sql_v1` 的 240 道题只依据本项目复制的数据库快照、字段注释和值分布生成，没有读取“匿名审核员-text2sql”的问题、Gold SQL、评测代码或方法。所有 Gold 都通过当前 SQL AST 安全门，并在只读 SQLite 上执行后固定结果指纹。

当前 240 题已由匿名审核员全量复核并通过 Phase 6 签名证书验证，manifest 为 `human_reviewed`、`release_eligible=true`。这只证明数据集具备进入晋升评测的资格，并不跳过候选指标、shadow、canary 或最终人工批准。

| 类别 | 数量 |
|---|---:|
| 单表投影与筛选 | 40 |
| COUNT 与分组 | 40 |
| 聚合与 Top-K | 40 |
| NULL 与存在性 | 40 |
| 多表 Join | 50 |
| 组合条件与子查询 | 30 |

同一个 SQL Skeleton 的所有题作为一个不可拆分组，按 60%/20%/20% 写入：

- `train.jsonl`：144 题；
- `validation.jsonl`：48 题；
- `sealed_holdout.jsonl`：48 题。

每个 Case 都调用同一套 `plan-first-text2sql-v3` 主链：五个 Agent 在 11 个固定节点内协作，非 Agent Harness 负责确定性门禁与只读执行。运行时只接收 `question`；Gold SQL、Gold 结果指纹、必需表列和关系只存在于评测器一侧，sealed holdout 的报告不输出问题和候选 SQL。

## 重建与校验

```bash
python scripts/build_text2sql_dataset.py
```

生成器会对 240 条 Gold 逐条执行：AST 解析、只读安全检查、Schema 白名单、EXPLAIN 和真实 SQLite 查询。三个 split 文件分别有 SHA-256，manifest 再固定全数据集指纹；任意一字节修改都会导致加载失败。

## 运行评测

默认只跑 validation 的前 `EVOAGENT_EVAL_MAX_CASES` 题，避免无意产生大量模型调用：

```bash
python scripts/run_text2sql_evaluation.py --split validation
```

显式跑完整 validation：

```bash
python scripts/run_text2sql_evaluation.py --split validation --max-cases 0
```

sealed holdout 只能通过同一评测入口运行：

```bash
python scripts/run_text2sql_evaluation.py --split sealed_holdout --max-cases 0
```

结果默认写入 `artifacts/text2sql/evaluation/latest.json`，并固定数据库、stable 知识索引、Vanna 索引、Memory、Policy、数据集、模型、温度和运行时间。

## 指标与失败归因

主指标是 Execution Accuracy：无排序语义按多重集合比较，有排序语义按顺序比较；NULL 与空字符串不同，BLOB 使用长度和 SHA-256，浮点数统一到六位小数。

同时输出：

- SQL 可执行率、AST 通过率、只读安全率；
- Table Recall、Column Recall、Join Edge Recall、Value Grounding Accuracy；
- P50/P95 延迟；
- 按 SQL Skeleton 分桶的 EX；
- `NO_SQL`、`PARSE_ERROR`、`UNSAFE_SQL`、`UNKNOWN_TABLE`、`UNKNOWN_COLUMN`、`SCHEMA_LINK_MISMATCH`、`FILTER_MISMATCH`、`AGGREGATION_MISMATCH`、`JOIN_OR_GRAIN_MISMATCH`、`EXECUTION_ERROR`、`TIMEOUT`、`UNEXPECTED_EMPTY`、`RESULT_MISMATCH`、`FRAMEWORK_ERROR` 等确定性分类。

当前 Join Catalog 仍未人工批准，因此 Join 题会诚实暴露 Join Edge Recall 和知识缺口。不能为提高分数把候选 Join 自动升为 stable。

## 自进化隔离要求

后续 Candidate 生成器只能读取 train 的失败摘要；validation 仅用于晋升判断；sealed holdout 只返回聚合指标和匿名 case_id。任何 holdout 问题、Gold SQL、结果或失败细节都不得写入 Wiki、Memory、Few-shot 或 Candidate Prompt。
