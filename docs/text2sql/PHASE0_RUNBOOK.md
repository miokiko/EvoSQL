# Text2SQL Phase 0 运行手册

## 目标与边界

评测数据只来自 `database/test1_full_20241118.sql`。数据库名固定为 `evo_text2sql_eval`，避免误导入现有业务库；应用账号固定为 `evo_text2sql_ro`，只有 `SELECT` 和 `SHOW VIEW`。

## 生成冷启动快照

这一步不依赖运行中的 MySQL：

```bash
python scripts/generate_text2sql_schema.py
```

输出位于 `artifacts/text2sql/schema/`：

- `database_snapshot.json`：表、列、主键、行数、NULL 比例、基数和值域；
- `join_candidates.json`：名称、主键和数据重合度生成的关系候选；
- `join_catalog.review.json`：人工确认文件，默认全部为 `pending`；
- `manifest.json`：上述三个内容工件的 SHA-256（manifest 不对自身做递归哈希）。

不得把候选 Join 直接当成已确认关系。审核时将 `decision` 改为 `approved` 或 `rejected`，并补齐基数、结果粒度、fanout 风险、审核人和说明。相同快照重新生成时保留已有审核字段；数据库快照变化时自动回到待审核。

## Docker 隔离导入（推荐）

先在 `.env` 中替换两个示例密码，再运行：

```bash
docker compose up -d text2sql-mysql
docker compose exec text2sql-mysql mysql -uevo_text2sql_ro -p evo_text2sql_eval -e "SHOW TABLES"
python scripts/verify_text2sql_mysql.py
```

容器只绑定 `127.0.0.1:3307`。首次创建数据卷时自动导入 dump，随后撤销应用账号写权限。若 dump 变化，需要创建新的评测卷或使用下面的显式 bootstrap 脚本；不要在原卷里静默替换数据。

## 已有 MySQL 的显式导入

管理员凭据只用于建库和导入，不提供给 EvoAgent：

```bash
python scripts/bootstrap_text2sql_mysql.py \
  --host 127.0.0.1 \
  --port 3307 \
  --admin-user root \
  --readonly-user evo_text2sql_ro
```

密码通过 `.env` 或当前进程环境变量提供，不写入命令历史。脚本完成后会输出表数和 `SHOW GRANTS`，不会打印密码。

## 使用 Navicat 已保存连接导入

当 MySQL 已运行且凭据只保存在 Navicat 时，先生成本地导入文件：

```bash
python scripts/prepare_navicat_import.py
```

然后在 Navicat 的 `rockburst` 连接中使用“执行 SQL 文件”，选择 `database/evo_text2sql_eval.local.sql`。该文件先创建并切换到 `evo_text2sql_eval`，所以源 dump 中的 `DROP TABLE` 不会作用到原 `test1`；导入结束后创建仅限本机的只读账号并输出表数与授权。生成文件含本地开发密码，已被 `.gitignore` 排除，不能提交或发送。

## 当前机器状态

2026-09-03 检查结果：本机未安装 Docker；Anaconda 附带的 `mysqld 8.4.0` 在初始化全新数据目录时发生 SIGSEGV。因此首版工件使用 dump 冷解析生成，真实导入命令和隔离配置已就绪，但需要可用的 Docker 或独立 MySQL 8 实例后才能完成在线验证。禁止改用现有业务数据库绕过此限制。
