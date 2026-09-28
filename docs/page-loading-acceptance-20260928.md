# 页面加载修复：手机真机验收

日期：2026-09-28。对应诊断：[page-loading-diagnosis-20260928.md](page-loading-diagnosis-20260928.md)。

## 安装与数据保护

- 设备：PLR-AL00，HarmonyOS API 26，USB HDC 目标 `3DK0225528040628`；仅安装 phone 模块。
- 使用工程文档要求的 DevEco JBR 和单次 ZIP64 兼容参数，实际绑定的 `default` 产品构建 phone 模块，签名 HAP 构建成功。安装包 SHA-256：`79F79FAB875B1881C58F4E5142F518B1019B185D2B16626FBDE42DF77FFB7F88`；ZIP 共 12 个条目，没有隔离测试资源。`hdc install -r` 返回 `install bundle successfully`，未卸载或清除应用数据。
- 安装前停止应用，只读导出 phone/entry 文件和 phone 数据库，共 1,999 个文件、3,803,475,013 字节；覆盖安装后再次导出，逐文件长度和 SHA-256 差异为 0。私有备份和校验清单保存在手机仓库忽略的 `outputs/page-loading-phone-acceptance-20260928/`。
- 启动后手机投影 schema 从 14 升至 15，转写仍为 8,136 条。最终再次只读导出数据库，按 `resource_id, revision, payload_json` 排序计算的转写事实摘要与安装前一致。

## 原生 UI 验收

DevEco Testing Python/Hypium 用例位于手机仓库 `tests/ui/testcases/PhonePageLoadingAcceptance.py`，运行器为 `tests/ui/run_page_loading.py`。最终报告：`tests/ui/reports/2026-09-28-14-00-48/summary_report.xml`，1/1 通过。用例启动正式应用，不使用隔离入口或合成数据库。

| 场景 | 真机结果 |
| --- | --- |
| 进入最长录音详情 | 会话有 1,673 条转写；首屏 UI 树仅有 2 条可见转写行 |
| 连续滚动 | 可见行保持 3–4 条；越过第 50 条的 3:37 时间位置后，显示到 5:59，后续页可读取 |
| 本地转写正文搜索 | 从首句正文动态取搜索词，录音列表找回同一长会话 |
| 30 秒后返回并重进 | 连续 10 次重进均成功，保留按需构建；可见代理耗时中位数 250 ms，10 次最高值 266 ms |
| 本地播放 | 启动与暂停成功；播放状态变化期间三次 UI 树均仅有 2 条可见转写行 |

“可见代理耗时”从 Hypium 发起点击到找到“转写时间线”，包含驱动命令与控件查询开销，**不是**点击到首帧的精确 P95。原始结果在忽略目录的 `hypium-results.json`。

一次独立 Hypium 运行同时采集了 `ace/app/graphic/sched` hitrace。解析到 420 个应用 `UIVsyncTask` 区间：P95 约 1.39 ms，最大约 16.82 ms，超过 50 ms 的该类区间为 0；17 个 `FrameNode[List]::RenderTask` 区间最大约 0.82 ms。原始 trace 与摘要在忽略目录的 `hypium-repeat.trace` 和 `hypium-repeat.summary.json`。这些区间不是完整帧或主线程全部工作，不能单凭它们宣称整页无长帧。

## 离线验收与剩余边界

- `svc wifi disable` 虽返回成功，但 `wlan0` 当时仍带 IP 且为 UP，不能据此认定离线。随后通过系统控制中心的 Wi‑Fi 开关断开连接，确认 `wlan0` 失去 IP 和 RUNNING 状态。独立 Hypium 离线用例通过，报告为 `tests/ui/reports/2026-09-28-14-11-33/summary_report.xml`：离线打开已缓存的 1,673 条会话、本地播放和 SQLite 转写搜索均成功。测试后用 `svc wifi enable` 恢复 Wi‑Fi，确认接口重新 UP/RUNNING 并取得 IP。
- 没有在真实同步进行中或执行人工标注写入时测量详情；本次重点是长列表、分页、搜索、缓存回访和播放。没有旧包的同设备同场景 trace 对照，也没有独立的点击到首帧、整帧掉帧和内存 P95 统计。
- 测试期间按 `doc/PHONE_SCREEN_ON_DEBUG.md` 临时设置常亮；结束时已用 `power-shell timeout -r` 恢复，`ScreenOffTime: Timeout=600000ms`，无覆盖值。

## 电脑端现场复验

手机验收后，电脑原有 WebUI 仍是 09:12 启动的旧 Python 进程。新前端请求 `/api/v3/person-choices` 时返回 404，人物页显示“无法读取本地 Core signal timed out”。在应用内浏览器复现后，用 SQLite backup API 保存正式数据库快照到忽略目录 `outputs/page-loading-desktop-acceptance-20260928/`。停掉旧 WebUI，只重启 8765 WebUI，保留 8766 手机接收服务；启动参数为 `web --port 8765 --no-open`，没有打开系统默认浏览器。数据库 schema 从 18 迁移到 20，新索引 `idx_utterances_active_speaker_track` 和 `idx_utterances_active_run_time` 已存在。

应用内浏览器复验：人物页能显示 9 人及详情；总结页的日报和人物选择器独立显示；审核页结束加载并显示空状态；录音库显示 21 条；长录音 1,673 条转写的时间线首批显示 80 条，继续加载到 160 条；全文搜索“我们”返回 69 条匹配。浏览器导航按钮使用键盘 Enter 可正确切换页面。自动点击曾命中相邻按钮，属于本次浏览器自动化命中现象，未作为产品缺陷结论。

脚本 `outputs/page-loading-desktop-acceptance-20260928/check_live.py` 对正式本机服务做 8 次暖态只读 HTTP 测量。测量包括本机会话恢复和传输，不是浏览器首帧时间；以下为中位数，最大值均小于 500 ms。

| 路径 | P50 | 最大 | 响应大小 |
| --- | ---: | ---: | ---: |
| 人物选择器 | 15 ms | 18 ms | 814 B |
| 完整人物列表 | 144 ms | 166 ms | 9.6 KB |
| 审核列表 | 141 ms | 150 ms | 7.1 KB |
| 总览 | 30 ms | 32 ms | 4.3 KB |
| 录音列表 | 30 ms | 32 ms | 14.2 KB |
| 会话概览 | 16 ms | 17 ms | 20.0 KB |
| 首批 80 条时间线 | 31 ms | 33 ms | 49.6 KB |

此次数据下人物与审核接口达到诊断文档建议的暖态 P95 < 500 ms 目标，但 8 次样本不足以建立长期 P95 保证。审核端仍需有大量待审项目的负载验收；当前数据的审核页面为空状态。
