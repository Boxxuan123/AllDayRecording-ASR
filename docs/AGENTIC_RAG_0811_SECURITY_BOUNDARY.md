# Agentic RAG 0.8.11：执行前能力边界

仅对 history-investigation-v1 加固，不改变人物规则、搜索策略或两轮调查协议。

- 模型请求经过同一受控 Windows Job 中的一次性 loopback 网关。网关重建应用授权的输入、既有协议和模型配置，丢弃 SDK 附加上下文及原生工具目录，固定 tools=[] / tool_choice=none。
- 固定上游为当前 ChatGPT Codex Responses 服务；凭据只在内存中转发，拒绝重定向。此实现面向当前 OpenAI/ChatGPT 配置，不承诺其他 provider 兼容性。
- SSE 事件在发送给 SDK 前进行正向类型检查。工具调用、未知事件、非文本部分均拒绝；每个网关最多一次上游请求，禁止隐式重试。最终结果检查只是第二道检查，不承担工具隔离。
- 应用调查工具在解析参数与引用之前检查八项只读工具白名单。模型的 JSON actions 仍由原有调查编排器执行，原有来源、证据、输入和时间上限不变。
- 新评测工具从空白 Core SQLite 和独立 ChatCache 开始，每例仅加载该例虚构 SQLite。源数据库以 mode=ro/query_only 读取；固定源接口拒绝同步、任意 URL、文件路径与跨案例记录 ID。无生产连接、人物别名、长期记忆或录音数据。
- 评测每例一次性锁、至多两次模型调用、每次 25 秒、整例 60 秒、全部 180 秒；父进程持有排他的 Windows Job。失败不重试，未授权的简单模型/规划备用路径直接拒绝。

预检使用独立合成安全探针，无真实推理请求：已安装 SDK 的正常合成响应能返回；恶意原生函数调用在 SDK 接收前被拒绝。数据库越界、只读写入、其他案例 ID、未授权路由、重复执行锁均有负向测试。

支持自定义 Responses provider 的官方配置说明：https://learn.chatgpt.com/docs/config-file/config-reference 。不把 hooks 视作完整能力边界，官方说明 hosted tools 和部分专用路径并非都受其覆盖：https://learn.chatgpt.com/docs/hooks 。

真实评测首次结果另见 AGENTIC_RAG_BLIND_EVALUATION.md；安全预检不等于服务兼容性或效果已通过。
