# 阶段 7：显式路径、配置与跨层 DTO

> 完成日期：2026-08-30
> 范围：建立可注入的应用路径对象，消除 backend 构造期缓存副作用，并固化关键 Web payload。

## 1. 显式路径对象

`allday_asr.paths.AppPaths` 现在集中描述：

- `state_dir`；
- `output_dir`；
- `model_dir`；
- `config_path`；
- `database_path`。

`AppPaths.from_environment()` 可以对任意环境映射按调用构造，不再要求测试先修改
进程环境再重新导入模块。目录创建、录音输出目录创建和模型缓存配置均是显式方法。
`load_config(paths=...)`、CLI runtime factory 和模型 backend 均支持注入同一个路径对象。

原 `STATE_DIR`、`OUTPUT_DIR`、`MODEL_DIR`、`DEFAULT_DB_PATH`、
`DEFAULT_CONFIG_PATH`、`EVALUATION_DIR` 及三个模块级 helper 暂时保留为兼容入口，
并统一委托给 `DEFAULT_PATHS`。待现有 service 的默认参数和外部脚本全部迁到
composition root 后，才删除这些别名。

## 2. 构造期副作用

以下 backend 构造函数现在只校验参数和保存依赖：

- FunASR/SenseVoice；
- Qwen3-ASR 与 Fun-ASR-Nano；
- 三个 oracle ASR backend；
- Pyannote Community-1。

构造对象不再创建 `models` 目录，也不再写 `MODELSCOPE_CACHE`、`HF_HOME`
或 `PYANNOTE_METRICS_ENABLED`。缓存和遥测配置延后到 `ensure_loaded()` 或第一次
实际模型加载之前；缓存快照查找也使用注入的 `model_dir`。

## 3. Web DTO 与序列化边界

Web presenter 新增 `ProcessingRunPayload`、`DashboardRunPayload`、
`ActionPayload`、`DailyStepPayload` 和 `DailyRunPayload` 五类 `TypedDict`。
route/use case 与 presenter 之间的关键字段由静态结构描述。

后台 daily job 不再自行 `asdict()` 和拼装 JSON-ready 字典，而是调用
`daily_summary_payload()`。HTTP JSON 编码继续只由 response transport 完成，原 API
字段和值保持不变。

## 4. 契约与兼容

新增测试覆盖：

- 两组临时 `AppPaths` 在同一进程内互不影响，构造过程不创建目录；
- 显式缓存映射不修改进程环境；
- 配置加载器和 CLI backend factory 使用注入路径；
- 七类模型对象构造时不创建缓存目录、不修改缓存或遥测环境变量；
- Web `TypedDict` 必填/可选字段和 daily presenter 输出。

已有默认路径、命令参数、模型加载时机之外的行为及 Web payload 均保持兼容。

## 5. 验证

- `.venv\Scripts\python.exe -m unittest discover -s tests -t .`：151 个测试通过；
- `.venv\Scripts\ruff.exe check src tests`：通过；
- `git diff --check`：通过。

本阶段没有读取真实录音、加载模型、修改业务数据库或访问网络。

## 6. 阶段结论

阶段 7 的四项验收条件已满足。可维护性指南定义的阶段 0 至阶段 7 已全部完成；
后续结构调整应按实际业务里程碑拆成新的独立工作包，而不是继续扩大兼容层。
