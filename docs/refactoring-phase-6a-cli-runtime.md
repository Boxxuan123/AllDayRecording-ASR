# 阶段 6A：CLI 运行时装配接缝

> 完成日期：2026-08-30  
> 范围：先把模型配置、settings 和 backend factory 的组合移出根 CLI，为后续按命令组拆文件建立稳定 composition root。

## 1. 改动

新增 `application` 外侧的 CLI adapter 包：

```text
interfaces/cli/
  __init__.py
  runtime.py
```

`runtime.py` 现在负责：

- 根据 `AppConfig` 和显存 profile 构造 `QualityAsrSettings`；
- 生成保持原字段与 canonical JSON 规则的模型签名；
- 构造 Qwen/Aligner、FunASR 和 Pyannote 的延迟 backend factory；
- 根据配置与命令行覆盖构造 `QualityDiarizationSettings`；
- 用只读 runtime dataclass 把 settings 与 factory 交给命令层。

根 `cli.py` 中原 `_runtime_signature`、`_quality_asr_runtime` 和 `_quality_diarization_runtime` 已删除。`workflow-v2 run`、`asr-v2 run` 和 `diarization-v2 run` 改为消费 `QualityAsrRuntime` / `QualityDiarizationRuntime`。

## 2. 可替换模型工厂

`RuntimeBackendBuilders` 显式承载四个构造器：speech gate、primary ASR、secondary ASR 和 diarization。默认构造器只在模型命令真正请求 runtime 时局部导入具体 backend；测试传入 fake builders 时不会创建、下载或加载真实模型。

这样 command 测试可以分别验证：

- profile、batch、模型 ID 和设备参数；
- speech gate 参数转换；
- CLI 覆盖说话人数和模型路径；
- backend factory 的延迟实例化。

## 3. 回归保护

新增 `tests/test_cli_runtime.py`，固定：

- ASR 与 diarization runtime 使用注入 builder；
- backend 参数和 settings 保持一致；
- runtime signature 与映射字段顺序无关；
- 根 CLI 和 runtime 模块均不在顶层导入四个具体模型构造器。

原 root help、Typer 参数错误、空库只读命令、doctor 退出码和架构测试继续通过。

## 4. 验证

- `.venv\Scripts\python.exe -m unittest discover -s tests -t .`：138 个测试通过；
- `python -m ruff check src tests`：通过；
- `git diff --check`：通过。

本批没有读取真实录音、加载模型、修改业务数据库或访问网络。

## 5. 后续结果

上述 session/workflow 与 V2 命令迁移已在阶段 6B 完成，详见
[阶段 6B command 分组记录](refactoring-phase-6b-cli-command-groups.md)。下一批继续
迁移 benchmark/library/legacy 命令并把根入口收缩为薄转发。
