# 可维护性重构阶段 2C：显式数据库生命周期

> 完成日期：2026-08-30
> 前置基线：阶段 2B，106 个测试通过
> 交付基线：110 个测试通过
> 范围：移除 `Database.__init__` 的目录创建和 migration 隐式 I/O

## 1. 结果

数据库生命周期现在明确分为四步：

```python
database = Database(path)       # 只解析路径并装配 repositories
database.initialize()          # 显式建目录、初始化或迁移 schema
with database.connect():       # 显式打开一次事务连接
    ...
repository = database.asr      # 取得窄 repository
```

普通运行入口使用等价的便捷工厂：

```python
database = Database.open(path)
```

`Database.open(...)` 只组合上述“构造 + 显式 initialize”两步。`initialize()` 保持幂等；
对空路径创建最新 schema，对历史库执行原有备份和迁移，对高于当前版本的数据库继续拒绝。

项目内 86 个调用点已迁移到 `Database.open(...)`，覆盖 CLI、Web 和全部测试入口。
`Database` 的 96 个兼容门面方法及八个 repository 属性均未改变。

## 2. 副作用边界

`Database.__init__` 不再：

- 创建数据库父目录；
- 创建 SQLite 文件；
- 打开数据库连接；
- 检查或执行 migration。

目录创建和 migration 只发生在显式 `initialize()`/`open()` 路径。一次业务事务仍由
`connect()` 的上下文管理器明确控制，提交、异常回滚、foreign key 和 WAL 设置保持不变。

项目外若直接使用存储模块，应把旧的 `Database(path)` 改为 `Database.open(path)`；若需要
先注入或检查 repository，则可先纯构造，再显式调用 `initialize()`。

## 3. 自动约束与兼容性

新增测试确保：

- 仅构造 `Database` 不创建 SQLite 文件，也不运行 migration；
- AST 架构约束禁止构造函数调用 `mkdir`、`connect`、`initialize` 或
  `MigrationRunner`；
- 运行时代码不得绕过 `Database.open(...)` 直接构造数据库门面；
- `Database.open(...)` 会初始化最新 schema，且重复打开保持幂等；
- 空库 schema、v1–v14 升级、升级前备份、失败回滚和高版本拒绝继续通过；
- CLI smoke、Web 路由、workflow 和 repository 事务行为保持不变。

当前验证基线：

```text
python -m unittest discover -s tests -v  -> 110 tests passed
ruff check src tests                      -> All checks passed
```

本阶段没有改变 schema、migration SQL、CLI 参数、HTTP payload、模型配置或数据边界，
也没有加载模型或读取 `data/` 中的真实录音。

## 4. 后续

阶段 2 的连接、migration、repository 和生命周期拆分全部完成。下一步进入阶段 3，按指南
拆分质量工作流，使阶段输入、复用、失败和审核语义可独立测试。
