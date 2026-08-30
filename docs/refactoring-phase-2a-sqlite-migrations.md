# 可维护性重构阶段 2A：SQLite 连接与迁移基础设施

> 完成日期：2026-08-30
> 前置基线：阶段 0 提交 `2be0e37`，阶段 1 工作树 92 个测试通过
> 范围：只提取 SQLite 连接、migration runner 与 v001–v015 历史 SQL

## 1. 结果

`Database` 的公开构造、`connect()`、`initialize()`、`schema_version()` 和全部仓储方法
保持不变。连接事务已移到 `infrastructure/sqlite/connection.py`，迁移顺序、最新
版本、高版本拒绝、升级前备份和失败回滚已移到 `migration_runner.py`。

历史 schema SQL 现按版本存放在：

```text
infrastructure/sqlite/migrations/
  v001_initial.py
  ...
  v015_manual_identity.py
```

迁移 SQL 没有整理或改写。拆分前后的 `SCHEMA` 与 v002–v015 SQL 按版本组合后
SHA-256 都是：

```text
8cc7cd5f9cb5b93249fa7de3fb68641cf6060b5f9027ce1b43622db9751a9ef3
```

`storage/database.py` 继续兼容 re-export `SCHEMA`、`MIGRATIONS` 和 guard SQL，供
既有测试及过渡期调用方使用，但不再定义 migration catalog。`LATEST_SCHEMA_VERSION`
只由 migration runner 根据已发布 migration 计算。

## 2. 迁移事务边界

每个 migration 及其 `schema_migrations` 版本记录现在位于同一显式事务。执行失败时
runner 回滚该 migration，既不保留其 schema 写入，也不记录对应版本。历史 v11 SQL
自带 foreign-key pragma 和事务语句；runner 保留原始 SQL 字节，在执行时由外层事务
统一管理这组边界，确保版本记录仍与 schema 修改原子提交。

原有兼容 hook 保持原顺序：v11 前补齐历史 v4 `processing_runs` 列，迁移完成后补齐
v4/v5 列、安装 v5/v7 guard，并回填 source graph。空库和旧库都经过同一个 runner。

## 3. Characterization tests

新增测试覆盖：

- v001–v015 已发布 SQL 的组合哈希固定；
- 空库初始化后的 43 张表、32 个索引、53 个 trigger 清单哈希固定；
- 每个历史版本 v1–v14 都能升级到 v15，并且各自产生一份升级前备份；
- 故意失败的 migration 不留下建表结果，也不写入版本 2；
- v16 数据库在任何 schema 写入前被拒绝；
- `Database` 不再重新定义 schema、migration manifest 或最新版本。

验证结果：

```text
python -m unittest discover -v  -> 98 tests passed in 21.529s
ruff check src tests             -> All checks passed
```

本阶段没有改变 schema 版本，没有读取 `data/` 中的真实录音，也没有加载模型。

## 4. 后续边界

阶段 2B 将从兼容门面中优先提取 Session、Run 和 Semantic repository。新业务代码应
依赖窄 repository；在调用方迁移完成前，`Database` 继续转发旧方法。构造函数中的
隐式初始化仍保留，按计划留到阶段 2C 单独处理。
