# 阶段 4A：Vue 前端迁移记录

> 完成日期：2026-08-30
> 范围：只迁移和拆分前端，并接通 Python 静态资源入口；后续阶段 4B 已完成后端拆分。

## 1. 迁移目标

旧工作台由 `web_assets/index.html`、约 2,600 行 `app.js` 和约 2,300 行
`styles.css` 组成。全局状态、API、音频播放、V2-D1/D2/D3 审核、评测、语义证据、
行动候选和运行记录共享同一脚本作用域，任何局部修改都可能影响其他页面。

本阶段使用仓库已有的 `all_day_recording_front/` Vue 3 + TypeScript + Vite 骨架，
把它设为唯一前端源码入口，同时满足以下兼容要求：

- `/`、`/assets/app.js`、`/assets/styles.css` 和全部 `/api/*` 不变；
- 临时 token、HttpOnly cookie 和回环地址认证不变；
- 说话人时间轴、人工身份、语义、评测、行动候选和任务轮询功能不丢失；
- `benchmark annotate-blind` 的独立盲标台继续工作；
- 最终用户运行 Python 服务时不需要 Node。

## 2. 源码边界

Vue SFC 将稳定页面区域拆成 layout 与五个业务视图：

- `SidebarNav.vue`、`WorkspaceHeader.vue`；
- `TimelineView.vue`、`EvaluationView.vue`、`SemanticView.vue`；
- `ActionsView.vue`、`RunsView.vue`。

行为代码拆为：

- `api/client.js`：同源认证、JSON 编解码和统一错误；
- `state/workspace.js`：会话工作区状态、查询参数和切换重置；
- `audio/playback.js`：元数据等待、seek 和区间裁剪；
- `utils/format.js`：时长、偏移、日期、指标和 workflow 文案；
- `views/*.js`：dashboard/session、timeline/identity、evaluation、semantic、actions、runs；
- `workspace/controller.js`：初始化、导航、会话聚合和高层任务连接。

控制器从迁移前约 2,600 行降至约 200 行。时间轴和身份审核虽然仍共享同一业务
模块，但不再与评测、语义或行动候选共享脚本内部状态。

## 3. 构建与发布

`npm run build` 先运行 `vue-tsc`，再由 Vite 生成稳定文件：

```text
src/allday_asr/web_assets/
  index.html
  blind.html
  favicon.svg
  icons.svg
  assets/
    app.js
    styles.css
    blind.js
    blind.css
```

生产产物保存在仓库并通过 `pyproject.toml` 进入 Python 包。`web.py` 只在解析后的
`web_assets` 根目录内读取静态文件，并按扩展名返回 MIME；`blind_web.py` 复用同一
构建目录。开发时执行：

```powershell
cd all_day_recording_front
npm test
npm run build
```

## 4. 验证结果

- `npm test`：4 个前端纯函数/状态测试通过；
- `npm run build`：Vue 类型检查和 Vite 生产构建通过；
- `python -m ruff check src tests`：通过；
- `tests.test_web` 与 `tests.test_blind_web`：静态资源、认证、API、音频 Range 和盲标台通过；
- 本机浏览器冒烟：真实既有会话的 dashboard、V2-D run 和 7 个候选成功加载；五个
  主视图逐一切换且始终只有一个活动视图；控制台无 warning/error。

浏览器冒烟没有触发 ASR、说话人或语义生成按钮，也没有修改审核数据。页面首次
展示候选时按既有行为读取窗口与派生试听资源。

## 5. 与阶段 4B 的衔接

阶段 4B 已把 `web.py` 收缩为兼容导出，并拆出 router/auth/responses/jobs、资源
route、presenter、application use case 和音频响应；详细结果见
[Web 后端模块化记录](refactoring-phase-4b-web-backend.md)。
