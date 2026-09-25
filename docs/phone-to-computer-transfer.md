# 手机到电脑文件传输协议

电脑端接收服务与只监听 `127.0.0.1` 的网页工作台相互独立。它只把手机已经保存的录音和会话清单按原相对路径接收到 `data/phone-inbox`，不会开放数据库或评测接口，也不会自动启动 ASR。

## 启动接收端

```powershell
allday-asr device receive
```

服务默认监听所有 IPv4 网卡的 `8766` 端口，并输出：

- 当前可用的局域网 HTTPS 地址；
- 首次配对二维码 `state/transfer-pairing.png`；
- 配对 CA 的 SHA-256 指纹和稳定接收端 ID；
- Passkey、设备公钥和最终接收目录。

二维码包含当前地址、配对 CA、CA 指纹、稳定接收端 ID 和一次性配对码。手机验证 CA DER 指纹后，用系统 Passkey 授权登记一个由 HUKS 生成的 P-256 设备公钥。配对码只能登记身份，不能查询或上传文件。

Windows 首次监听时可能弹出防火墙提示。若只在家庭网络使用，允许专用网络即可；若希望切换到被 Windows 标记为“公用网络”的 Wi-Fi 后仍能自动发现，应一次性同时允许公用网络。接收端仍要求配对 CA、设备签名和请求 challenge，但不要把端口映射到公网。也可以显式选择端口和目录：

```powershell
allday-asr device receive `
  --port 8766 `
  --inbox D:\AllDayRecording-Inbox
```

首次启动会在 `state/transfer-tls` 创建持久配对 CA，并为当前局域网地址签发服务证书；已登记 Passkey 保存在 `state/transfer-passkeys.json`，设备公钥保存在 `state/transfer-devices.json`。这些文件都不包含手机私钥。

接收端运行期间会监测网卡地址变化：Wi-Fi 改变后重新签发由同一 CA 信任的当前 IP 证书，并更新 `_allday-pc._tcp.local.` mDNS/DNS-SD 广播。手机按稳定 `receiver_id` 找到新地址，再用原 CA 验证 TLS，因此正常换 Wi-Fi 不需要修改配置或重新认证。

`state/transfer-tls/receiver-ca-key.pem` 是电脑的私密配对身份，不得上传或分享。删除、替换或损坏配对 CA，以及清除手机 HUKS 私钥，都会要求明确重新配对。自定义正式证书也受支持：

```powershell
allday-asr device receive `
  --tls-cert D:\certs\receiver-cert.pem `
  --tls-key D:\certs\receiver-key.pem
```

明文模式仅为回环协议测试保留：

```powershell
allday-asr device receive --host 127.0.0.1 --insecure-http
```

它不能绑定局域网地址，也不能用于真实录音。

## 首次 Passkey 与设备登记

手机先用配对码申请标准 WebAuthn 登记选项：

```http
POST /api/v1/passkeys/register/options
Authorization: Bearer <首次配对码>
Content-Type: application/json

{"device_name":"My Harmony phone"}
```

手机调用 HarmonyOS Passkey 创建能力，并在验证请求中同时提交 HUKS 设备公钥：

```http
POST /api/v1/passkeys/register/verify
Authorization: Bearer <首次配对码>
Content-Type: application/json

{
  "ceremony_id": "<登记流程 ID>",
  "credential": {"id":"...","rawId":"...","type":"public-key","response":{}},
  "device": {
    "name": "My Harmony phone",
    "algorithm": "ECDSA_P256_SHA256",
    "public_key": "<HUKS exportKeyItem 输出的无填充 base64url>"
  }
}
```

电脑验证 Passkey 的 challenge、origin、RP ID、用户在场和签名后，将 HUKS ECC 公钥材料规范化为 P-256 DER SubjectPublicKeyInfo 再保存。HarmonyOS Passkey 要求 RP 域名已在 AppGallery Connect/App Linking 中与应用关联；默认 `alldayrecording.local` 只是占位值，真机联调应配置真实稳定域名：

```powershell
allday-asr device receive `
  --passkey-rp-id transfer.example.com `
  --passkey-origin https://transfer.example.com
```

## 日常设备签名认证

Passkey 只用于登记或重新授权。日常上传先申请无需用户交互的一次性设备挑战：

```http
POST /api/v1/devices/authenticate/challenge
Content-Type: application/json

{"device_id":"<稳定设备 ID>"}
```

手机用 HUKS 私钥签署以下 UTF-8 原文；各字段以单个换行分隔，最后没有额外换行：

```text
ALL_DAY_RECORDING_DEVICE_AUTH_V1
<challenge_id>
<nonce>
<大写 HTTP method>
<只含 path 的接口路径>
<原始请求 body 的小写 SHA-256>
<PUT 的十进制 Upload-Offset；其他请求为空>
```

实际请求携带：

```text
X-AllDay-Device-ID: <device_id>
X-AllDay-Device-Challenge: <challenge_id>
X-AllDay-Device-Signature: <HUKS ECDSA 签名的无填充 base64url>
```

Challenge 有效期为 120 秒且只消费一次。电脑同时验证方法、路径、请求正文 SHA-256 和分片 offset，因此签名不能重放、替换正文或挪到另一个分片。旧版 Passkey 逐请求认证暂时保留为协议兼容路径，但新版手机不再调用它。

## 文件传输契约

所有请求都走已验证配对 CA 的 HTTPS，并使用上面的设备签名。协议版本为 `2`，只接收：

- `recording`：`.wav`、`.m4a`、`.flac`、`.aac`、`.opus` 或 `.ogg`；
- `manifest`：`.json`。

相对路径必须使用 `/`，不能包含绝对路径、`.`、`..`、隐藏目录或 Windows 非法文件名。接收端绝不覆盖同路径的不同内容。

### 探测服务

```http
GET /api/v1/status
```

### 创建或恢复上传任务

```http
POST /api/v1/uploads
Content-Type: application/json

{
  "relative_path": "pcm_session_1787972654273/segment_000001.wav",
  "size": 1920044,
  "sha256": "<完整文件 SHA-256>",
  "kind": "recording"
}
```

新任务返回 `201`；相同路径、大小和哈希的任务返回 `200`。响应中的 `offset` 是下一段应发送的位置，任务 ID 由元数据稳定生成。

### 查询并续传

```http
GET /api/v1/uploads/<upload_id>
```

```http
PUT /api/v1/uploads/<upload_id>
Content-Type: application/octet-stream
Content-Length: <分片字节数>
Upload-Offset: <分片起始偏移>

<原始文件字节>
```

单个请求默认不超过 4 MiB。每片落盘并 `fsync` 后才返回新 offset；最后一片经过完整 SHA-256 校验后原子发布。偏移冲突返回 `409` 和电脑期待的 `Upload-Offset`，哈希失败返回 `422` 并清理临时内容。

手机以 `ReceivedRecordingFile.sourcePath` 恢复原 Watch 会话路径，先传所有音频，最后生成并上传兼容 `AllDayRecording session manifest v1` 的 `session_summary.json`。手机原文件始终保留。`manifest` 完成 SHA-256 校验就是整个会话已到齐的提交信号。

接收端始终在该提交点把清单原子导入 V3 Core；自动处理默认开启，随后执行“备份并回读校验 → V3 原生模型分析 → 开放集说话人身份 → Codex 语义/提醒/洞察”。HTTP 最后一片在任务持久加入后台串行队列后立即返回，手机不等待模型。未显式给出备份位置时使用 `state/v3/session-backups`；正式部署建议用 `--workflow-backup-root` 指向真正的独立磁盘或网络位置。没有可由电脑复核的独立备份时，也可以显式选择 shadow。shadow 允许模型执行，但不会解除独立备份准入阻塞，结果不能当作 production 准入证据：

```powershell
allday-asr device receive --auto-process --workflow-shadow
```

正式 production 推荐给出真正的独立磁盘或网络备份位置；接收端会先逐文件备份、复算 SHA-256 并完成恢复演练，通过后才启动模型：

```powershell
allday-asr device receive --auto-process `
  --workflow-backup-root E:\AllDayRecordingBackup `
  --workflow-backup-storage-kind independent_device
```

自动任务按会话串行执行，避免多个 PyTorch 工作流争用 GPU。任一阶段失败后会依次等待 5 秒、30 秒和 120 秒自动重试；三次仍失败时，Desktop 工作台“审核收件箱”会显示“流程失败”和“重试完整流程”按钮。接收服务重启时会恢复正在运行、等待重试以及已完成上传但尚未处理的会话；清单导入和 V3 processing job 均按现有指纹规则复用，因此会从可复用阶段继续而不是重复整条流程。进度和跨进程重试请求持久保存在 V3 state 的 `automation` 目录。上传响应在 manifest 完成时还会包含 `v3.session_id`、导入状态和 `v3.automation` 当前任务快照。

`--auto-workflow` 仅作为 `--auto-process` 的兼容别名保留。只有显式使用 `--no-auto-process` 才会关闭自动流程；此时 manifest 仍会自动入库。从上传响应的 `v3.session_id` 取得会话 ID 后，可以显式完成备份准入、提交任务并运行 worker：

```powershell
allday-asr backup-admit <session-id> `
  --backup-root E:\AllDayRecordingBackup `
  --storage-kind independent_device
allday-asr process-submit <session-id>
allday-asr worker --once
```


## 同步进度与小文件优化（2026-09）

手机音频采用简单 AIMD 动态并发：初始 3，范围 1–16，健康窗口逐步加 1；重试、超时、吞吐或耗时明显恶化时减半，拥塞降档下限 2。同一文件分片仍串行；降档不重启在途文件，失败会停止派发并等待在途文件结束，所有音频完成后才提交会话清单。
失败会停止派发新文件，并等待在途请求结束，避免重试与旧请求重叠。网络响应丢失时先查询断点，确认已落盘的分片不会盲目重传；请求依旧逐次签名、使用一次性 challenge。

两个手机上传入口均跳过已有成功回执的完整会话。成功回执按会话持久保存，后续会话失败不会撤销之前的成功状态。界面显示会话时间、并行文件、文件计数、字节进度；已完成会话停止转圈。

电脑会话入库只校验该清单引用的录音；重复提交已经入库的同一清单复用持久结果。新文件的 SHA-256、fsync 和禁止覆盖不同内容的规则不变。V3 数据库第 12 次迁移补发缺失 session_key 的会话投影；后续状态更新始终从原清单带上关联键，修复手机本地录音与电脑投影重复显示。
