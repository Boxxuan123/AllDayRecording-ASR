# 阶段 6B：Session / Workflow / V2 命令分组

> 完成日期：2026-08-30  
> 范围：把 session、workflow-v2、asr-v2、diarization-v2 和 semantic-v2 从根 CLI 迁入独立 Typer command 模块。

## 1. 目标结构

```text
interfaces/cli/
  runtime.py
  targets.py
  commands/
    sessions.py
    workflow.py
    asr.py
    diarization.py
    semantic.py
```

五个 command 模块各自创建一个 Typer 子应用，只导入本组所需的 application/service。根 `cli.py` 导入并挂载子应用，不再定义这 22 个命令。

## 2. 保持的 CLI 协议

迁移保持原有：

- 子命令组名称：`session`、`workflow-v2`、`asr-v2`、`diarization-v2`、`semantic-v2`；
- 各命令名称、参数位置、选项名称、默认值、范围限制与 help 文本；
- Typer 参数错误和业务失败的退出码；
- Rich 表格、状态文本和警告输出；
- `.project.scripts` 的 `allday-asr = allday_asr.cli:app` 入口。

共享的 recording/session 选择规则迁入 `interfaces/cli/targets.py`，继续抛出同样的 `typer.BadParameter` 文本。

## 3. 依赖边界

command 模块不再继承根 CLI 的全部业务导入：

- `sessions.py` 只依赖 manifest、backup、readiness；
- `workflow.py` 只依赖质量 workflow、semantic settings 与 runtime；
- `asr.py` 只依赖质量 ASR 与 ASR runtime；
- `diarization.py` 只依赖 diarization application 能力和时间解析；
- `semantic.py` 只依赖 semantic application 能力。

根 `cli.py` 从阶段 6 开始前的 2467 行降至 1239 行。

## 4. 回归保护

新增 `tests/test_cli_commands.py`，固定：

- 五个子应用的现有命令名称仍出现在 help；
- run/build 的关键选项仍可见；
- 根 CLI 不再重新定义已迁移命令；
- 每个 command 模块只导入自己的 service 家族；
- 共享 target 解析保持缺失目标、legacy recording 映射和冲突校验。

## 5. 验证

- `.venv\Scripts\python.exe -m unittest discover -s tests -t .`：143 个测试通过；
- `python -m ruff check src tests`：通过；
- `git diff --check`：通过。

本批没有读取真实录音、加载模型、修改业务数据库或访问网络。

## 6. 后续结果

上述剩余命令迁移、统一 app 和根入口收缩已在阶段 6C 完成，详见
[阶段 6C CLI 收尾记录](refactoring-phase-6c-cli-completion.md)。阶段 6 已整体完成。
