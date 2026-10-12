# AllDayRecording-ASR

本地优先的全天录音处理工作台：接收手机上传的录音和会话清单，在本机完成双 ASR、说话人分析、可追溯文字时间线、人物记忆、提醒与每日洞察。

当前产品线为 V3.9：

- 所有运行时数据只写入 `state/v3`，Desktop API 只监听 loopback。
- 手机上传通过 TLS、Passkey 和 HUKS 设备签名认证，支持断点续传和 SHA-256 校验。
- 处理任务先持久化，生产模式下必须先通过独立备份与回读校验。
- 文字、人物、提醒和洞察都保留不可变证据、revision 与过期重算链路。
- Codex 只处理允许的文字证据，不上传音频或本地路径。

## 环境

- Windows、Python 3.12、Node.js 20+。
- FFmpeg / FFprobe 可用。
- 模型依赖见 `pyproject.toml`；PyTorch / torchaudio 请按显卡和 CUDA 环境单独安装。

```powershell
python -m pip install -e . --no-deps
```

## 主要入口

```powershell
# V3 Desktop 工作台
allday-asr web

# V3 Core 状态与数据库迁移
allday-asr status
allday-asr migrate

# 手机文件接收和增量同步；会话到齐后默认自动备份并跑完整流程
allday-asr device receive

# 可选：把自动备份放到指定的独立磁盘或网络位置
allday-asr device receive --auto-process `
  --workflow-backup-root D:\AllDayRecording-Backup

# 本地 shadow：允许模型执行，但不会解除独立备份准入阻塞
allday-asr device receive --auto-process --workflow-shadow
```

自动流程失败后会按 5 秒、30 秒、120 秒依次重试；仍失败的会话会进入
Desktop 工作台的“审核收件箱”，可点击“重试完整流程”。状态和重试请求均持久化，
接收服务重启后会从未完成阶段继续。仅在诊断时才使用 `--no-auto-process` 关闭自动流程。

手机上传与安全边界见 [手机到电脑文件传输协议](docs/phone-to-computer-transfer.md)。

## 前端

```powershell
cd all_day_recording_front
npm install
npm run dev
npm run test
npm run build
```

`npm run build` 会完成 TypeScript 校验，并把产物写入 `src/allday_asr/v3/web_assets`。

## 验证

```powershell
python -m ruff check src tests
python -m pytest tests

cd all_day_recording_front
npm run test
npm run build
```

## 设计文档

- [V3 重构规格](docs/V3/V3重构规格.md)
- [V3 升级功能](docs/V3/V3升级功能.md)
- [V3.4 开放集说话人身份](docs/V3/V3.4-open-speaker-identity.md)
- [V3.5 跨天人物记忆](docs/V3/V3.5-cross-day-person-memory.md)
- [V3.6 每日总结与关系观察](docs/V3/V3.6-daily-summary-and-relationship-observation.md)
- [V3.7 分层说话人身份与防污染学习](docs/V3/V3.7-layered-speaker-identity.md)
- [Computer 端超阈值模块维护计划](docs/V3/computer-maintainability-plan.md)

旧版实现不再作为可执行路径保留；需要追溯时使用 Git 历史。

## 许可

本仓库公开源码仅供查看，并非开源软件。除 GitHub 服务条款和适用法律明确要求的
权利外，不授予使用、复制、修改、再分发、商业化或创建衍生作品的许可。完整条款见
[LICENSE](LICENSE)。

## 独立聊天事项与手机提醒

Windows 的 `/followups` 页面支持跨作业接纳与新进展审核。升级到 Phone 1.0.4 后，在现有“更多→提醒”页同步查看、编辑、完成／忽略／重新开启，以及采用／拒绝变化；“启用提醒”要求确认当前仍需执行及明确的未来时刻，无需录音任务 ID。共享契约 3.9.0、投影 6、Core 33、Phone 20；两端一起升级。实机闭环本轮 NOT_RUN，完整合成闭环、迁移和操作步骤见 [2026-10-11 验收记录](docs/CHAT_FOLLOWUP_PHONE_ACCEPTANCE_20261011.md)。

## 聊天＋录音统一每日回顾 V1

电脑“总结与关系”页选择日期，顶部查看统一回顾；只生成所选自然日，复用已有结果。时间补丁、证据入口、预算、兼容性和验证限制见 [脱敏验收记录](docs/UNIFIED_REVIEW_ACCEPTANCE_20261011.md)。

零模型本地修订、互动优先和内容证据核验见 [2026-10-12 内容修补验收](docs/UNIFIED_REVIEW_CONTENT_ACCEPTANCE_20261012.md)。


跨天人物与事情记忆 V1：电脑“人物”与“总结与关系”展示固定范围的原文互动、具体事情时间线和独立话题讨论；手机“人物 → 跨天互动与事情”复用离线投影与操作队列。ASR 0.7.0 / Phone 1.0.8 / 契约 3.10.0 / 投影 7 / Core 34 / Phone 21，两端一起升级。详细入口、修订语义和验证边界见 [V1 验收与运行说明](docs/CROSS_DAY_MEMORY_V1_20261012.md)。
