# 阶段 6C：CLI 模块化收尾

> 完成日期：2026-08-30  
> 范围：迁移剩余 benchmark/evaluation/library/utility/legacy 命令，建立统一 Typer app，并把根 CLI 收缩为兼容入口。

## 1. 最终结构

```text
interfaces/cli/
  app.py
  runtime.py
  targets.py
  output.py
  commands/
    sessions.py
    workflow.py
    asr.py
    diarization.py
    semantic.py
    benchmark.py
    evaluation.py
    library.py
    utility.py
    legacy.py
```

`app.py` 只创建根 Typer app，并注册十个命令模块。`allday_asr/cli.py` 只从该模块重导出 `app`，继续兼容 `allday-asr = allday_asr.cli:app` 和 `python -m allday_asr.cli`。

## 2. 本批迁移

- benchmark：连续时间真值、盲标、预测快照、oracle ASR、评测与比较；
- evaluation：V1 人工真值模板和评测；
- library：人物声纹库同步、状态、积累和登记；
- utility：Web、配置、source 审计、逻辑窗口、daily-run 和 action review；
- legacy：doctor、V1 ingest/process/diarize、本人声纹、timeline、export 和 clip。

阶段 6A 的 runtime composition root 继续管理质量 ASR/diarization backend；本批又把 oracle ASR 工厂改为 runtime 内局部导入。测试或命令模块可以替换工厂而不在 help/import 阶段加载具体模型实现。

公共数值显示 `_metric` 迁入 `output.py`，evaluation 与 benchmark 共用同一 `N/A`/四位小数规则。

## 3. 规模与边界

- 根 `cli.py`：2467 行降至 9 行；
- 统一 `interfaces/cli/app.py`：33 行；
- 63 个命令全部由独立 command 模块注册；
- command 模块只导入自己的业务 service 家族；
- 根兼容入口不再定义命令函数或导入业务服务。

## 4. 兼容与测试

CLI 回归测试覆盖：

- 全部命令组名称及代表性命令；
- 根 utility/legacy 命令名称；
- run/build 的关键选项与 help；
- 原 Typer 参数校验和 doctor 失败退出码；
- 空库只读命令；
- command service 依赖白名单；
- 根 CLI 仅导入统一 app；
- 具体 Qwen/FunASR/Pyannote/oracle 构造器不在 CLI 顶层导入。

## 5. 验证

- `.venv\Scripts\python.exe -m unittest discover -s tests -t .`：144 个测试通过；
- `python -m ruff check src tests`：通过；
- `git diff --check`：通过。

本批没有读取真实录音、加载模型、修改业务数据库或访问网络。

## 6. 阶段结论

阶段 6A 完成 runtime composition root，6B 完成 session/workflow/V2 命令组，6C 完成剩余命令与根入口收缩。阶段 6 验收条件已满足，下一步进入阶段 7：显式路径、配置与跨层 DTO。
