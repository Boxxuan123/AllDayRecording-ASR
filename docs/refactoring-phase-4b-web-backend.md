# 阶段 4B：Web 后端模块化记录

> 完成日期：2026-08-30
> 范围：拆分本地工作台和盲标台的传输层、application use case 与后台任务接缝；不更换 HTTP 框架，不改变前端协议。

## 1. 重构目标

重构前的 `web.py` 超过 1,600 行，同时承担数据库查询聚合、业务写操作、后台线程、
音频裁剪、认证、静态文件、JSON 响应和全部 GET/POST/PUT 分支。任何新增端点都必须
修改同一个类，业务操作也无法脱离 HTTP handler 单独维护。

阶段 4B 保持现有 URL、方法、token/cookie、状态码与 JSON 字段不变，把边界收敛为：

- server 只处理 HTTP 生命周期与异常到状态码的映射；
- router 只做认证门禁和资源路由分发；
- route 只解析参数、调用 application use case、发送稳定 payload；
- presenter 只转换 DTO/数据库行；
- job registry 只保存线程安全的任务状态与进度；
- 音频 Range、普通文件、静态资源和 JSON 响应各自独立。

## 2. 最终结构

```text
src/allday_asr/
  web.py                         # 兼容导出
  interfaces/web/
    application.py               # 依赖装配、数据库入口和共享锁
    server.py                    # HTTP handler/server/create/serve
    router.py                    # GET/POST/PUT 分发
    auth.py                      # token、cookie、Origin
    responses.py                 # JSON、静态文件、安全响应头
    params.py                    # query/body/path 参数
    jobs.py                      # JobRegistry
    audio.py                     # byte Range 响应
    presenters.py                # 稳定 API payload
    routes/
      assets.py
      workspace.py
      evaluation.py
      timeline.py
      semantic.py
      actions.py
      audio.py
    use_cases/
      workspace.py
      evaluation.py
      timeline.py
      semantic.py
      background.py
      media.py
```

`WebApplication` 从约 900 行收缩到 45 行，只装配六类 use case、数据库路径、
`JobRegistry` 和真值/音频锁。`allday_asr.web` 保留
`WebApplication`、`AllDayRequestHandler`、`AllDayHTTPServer`、
`create_web_server` 与 `serve_web`，因此 CLI 和既有调用方无需迁移。

盲标台继续保留自己的任务业务逻辑，但与主工作台共享 token/cookie 认证、安全响应、
静态路径约束和音频 Range 实现。

## 3. 协议与安全兼容

- 未认证读取和写操作继续返回 `403`；
- mutation 继续校验同源 `Origin`，跨站请求返回 `403`；
- 缺失/非法参数返回 `400`，未知资源和 job 返回 `404`；
- 静态文件只能从解析后的 `web_assets` 根目录读取；
- 单段音频 byte Range 返回 `206` 和正确 `Content-Range`；
- 后缀 Range 可用，越界 Range 返回 `416` 与 `bytes */<size>`；
- CSP、frame、referrer 和 content-type 安全响应头保持不变。

## 4. 测试与验证

`tests/test_web_interfaces.py` 为参数解析、七类资源 route、router/auth 和
`JobRegistry` 增加独立测试，覆盖成功、参数错误、未授权、跨源、未知路径以及
queued/running/completed/failed/missing 状态。

集成测试继续通过真实本地 HTTP server 验证静态资源、cookie 登录、工作区 payload、
评测更新和盲标流程，并新增：

- 主工作台非法参数、未知 job/route、未认证 mutation 和跨源 mutation；
- 盲标音频普通 Range、后缀 Range、`206` 边界和越界 `416`。

最终验证结果：

- `.venv\Scripts\python.exe -m unittest discover -s tests -t .`：123 个测试通过；
- `python -m ruff check src tests`：通过；
- `git diff --check`：通过；
- `npm test`：4 个前端纯函数/状态测试通过；
- `npm run build`：Vue 类型检查和 Vite 生产构建通过；
- 本机浏览器：生产资源成功打开既有会话，五个主视图逐一切换且始终只有一个活动
  视图，控制台无 warning/error。

浏览器冒烟只读取现有会话和按页面既有行为请求派生试听缓存，没有点击运行、生成或
审核写入操作。

## 5. 阶段结论

阶段 4 已整体完成：前端源码不再集中在单个脚本，后端入口也不再混合传输协议与全部
业务操作。下一步按维护指南进入阶段 5，收拢版本化 semantic 与 diarization 代码，
同时继续保留历史协议和 run 重放能力。
