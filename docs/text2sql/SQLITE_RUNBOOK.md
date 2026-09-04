# SQLite 本地评测库

SQLite 是当前本地 Text2SQL MVP 的默认执行后端。唯一源数据仍是复制的 MySQL dump；转换器不读取 Navicat 配置，也不复用其他 Text2SQL 项目代码。

## 构建

```bash
python scripts/build_text2sql_sqlite.py
```

如果源 dump 确认发生变化并需要显式重建：

```bash
python scripts/build_text2sql_sqlite.py --replace
python scripts/generate_text2sql_schema.py
```

转换过程使用临时文件，完成校验后才原子替换目标库。默认拒绝覆盖已有 SQLite 文件。最终文件权限设置为只读，并通过以下两层约束打开：

```text
SQLite URI: mode=ro&immutable=1
PRAGMA query_only=ON
```

## 输出

- `database/evo_text2sql_eval.sqlite3`：20 张业务表和 562 行数据；
- `database/evo_text2sql_eval.sqlite3.manifest.json`：源 dump、Schema Snapshot 和 SQLite 文件指纹；
- `artifacts/text2sql/schema/database_snapshot.json`：保留 MySQL 原字段类型、注释和值域，供知识检索使用。

数据库文件和本地 Navicat 导入文件均被 `.gitignore` 排除。manifest 与 Schema Snapshot 可以进入版本管理，用于复现评测环境。

## 方言边界

本地基线的 SQL Policy 固定为 `sqlite`，评测 Gold SQL 也必须使用 SQLite 可执行语法。未来切换 MySQL 时需要创建新的 Policy/Dataset 版本，不得用同一指标直接混评两个方言。
