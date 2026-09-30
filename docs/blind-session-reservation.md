# Session reservation

Schema 23 在 recording_sessions 插入事务内用数据库触发器分配 dataset_role。
角色先于所有学习入口可见；同步、重复插入、重处理不会重抽。历史 session
回填 learning + historical_diagnostic_only，并登记历史暴露；它们不能再声称
无偏 blind。新 session 使用 session-counter-v1：持久化序号乘以 6181，模
10000 后按比例分流。默认 learning 70%、blind 25%、holdout 5%，collection
默认 OFF。分流与人物、embedding、真值无关；settings revision 与分配配置
快照持久化。比例不是每个小批次的数量保证。

在项目根目录、现有 Python 环境中：

```powershell
python tools/blind_session.py status
python tools/blind_session.py enable-collection
python tools/blind_session.py disable-collection
python tools/blind_session.py configure --policy-version session-counter-v2 --blind-ratio 0.25 --holdout-ratio 0.05
```

Collection ON 时所有**新建** session 都是 blind。关闭后恢复确定性比例。
既有/追加 session 的角色保持原值；为了收集独立证据，创建新的录音 session。
设置变更有 actor、revision 和审计；角色、policy、assigned_at、frozen_at 在
session_dataset_roles 与 session_role_audit 可查询。

样本 enqueue/claim/plan/publication、confirmed enrollment、prototype admission、
profile/anonymous reference/calibration 查询和 purity candidate 都要求 learning。
SQL 触发器承担最终防线，避免新入口绕过 repository gate。非 learning 的人物
事实仍可同步，但不会生成学习样本。历史/已暴露 learning 不能改成 blind；
返回 cannot_promote_to_blind。blind/holdout 不允许直接转回 learning。

```powershell
python tools/blind_session.py open-holdout --session <local-id> --decision-version future-study-v2 --reason "explicit future study decision"
```

该操作只记录指定未来研究的显式许可，不修改角色、不接入当前实验、不加入
enrollment。没有自动解锁。旧 blind 回收需要退休原实验、版本化新数据集并
另行实施受审计的回收流程；本轮没有回收入口。

本机认证 API：GET /api/v3/blind/status；GET /api/v3/blind/report。
设置写操作通过本机 CLI，避免新增网络管理授权面。
