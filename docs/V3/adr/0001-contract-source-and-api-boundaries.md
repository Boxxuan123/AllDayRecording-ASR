# ADR-0001：V3 契约源与 API 边界

- 状态：Accepted
- 日期：2026-08-31
- 适用版本：V3.0+

## 决策

`contracts/v3/manifest.json` 是 V3 契约版本入口，目录内 JSON Schema 和
OpenAPI 是 Python、Desktop Vue 与 Phone ArkTS 的唯一 wire contract 源。
客户端允许提交生成代码快照，但测试必须用规范 fixture 验证，不能手工扩展
服务端 enum 或资源字段。

Desktop API 固定在 `/api/v3/*`，仅供 loopback Desktop frontend 使用；Device
API 固定在 `/device/v3/*`，只允许配对设备签名与明确 scope。两个 API 使用不同
路径、认证 scheme、DTO 和授权矩阵，即使最终调用同一个 application core，也
不建立“两边都能调用”的管理接口。

服务端只产生 schema 已声明的 enum。旧客户端收到未来 enum 时显示本地
`unknown` 状态并保留原响应用于诊断，不因反序列化失败丢掉整批同步数据。

## 原因

V3.0-A 的首要风险是电脑、Vue 和 ArkTS 各自发明状态词与分页/错误结构。契约先行
能让 V3.0-B–F 在独立演进时仍以相同 ID、时间、revision、状态和同步信封互通。

## 后果

- 任何 wire 变更先修改 canonical schema/OpenAPI 与 fixture，再更新客户端快照。
- 兼容变更提升 contract minor/patch；破坏性变更必须使用新 major API path。
- OpenAPI 不承诺当前已有服务器实现；V3.0-A 只冻结接口骨架。
