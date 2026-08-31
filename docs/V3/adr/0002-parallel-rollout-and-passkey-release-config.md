# ADR-0002：V3 并行启用与 Passkey 发布配置

- 状态：Accepted
- 日期：2026-08-31
- 适用版本：V3.0+

## 决策

V3 与 V2 使用并行目录和显式 feature flag。`ALLDAY_V3_ENABLED` 默认关闭；
V3.0-A 的 `v3 start` 只装配空 runtime、读取契约 manifest，不打开 V2 SQLite、
不创建 V3 数据库、不监听端口，也不改变现有 V2 命令和页面入口。

Passkey 发布值只从发布环境注入：

```text
ALLDAY_V3_DEPLOYMENT=production
ALLDAY_V3_PASSKEY_RP_ID=<与 App Linking/AGC 绑定的真实域名>
ALLDAY_V3_PASSKEY_ORIGINS=<可信 ohos:app-id:... 或 HTTPS origin，逗号分隔>
```

Production 配置缺失时必须 fail closed。RP ID 为 IP、单标签主机，或使用
`.local`、`.localhost`、`.test`、`.example`、`.invalid` 后缀时一律拒绝启动。
开发模式允许暂不配置 Passkey，因为 V3.0-A 空 runtime 不暴露 Device listener。

## 原因

V2 现有传输配置仍需可回退，不能为 V3 草率改写其默认值；同时真机发布不能把
开发占位 RP ID 固化进凭据，造成 App Linking 失败或已经登记的凭据不可迁移。

## 后果

- 发布流水线必须显式注入并验证生产 RP ID/origin。
- 后续 V3 Device Gateway 复用现有 CA、receiver/device identity 与 HUKS 密钥，
  但不能把 V2 的开发默认值当成生产发布配置。
- V3 设为默认入口只能在 V3.0-G 发生。
