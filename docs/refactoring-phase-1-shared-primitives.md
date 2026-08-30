# 可维护性重构阶段 1：共享纯函数与依赖接缝

> 状态：完成
> 完成日期：2026-08-30
> 前置基线：阶段 0 提交 `2be0e37`

## 1. 结果

本阶段只移动既有算法和依赖入口，没有修改模型参数、数据库 schema、HTTP/CLI
协议或业务阈值。共享算法现在有明确归属：

| 能力 | 新入口 | 旧兼容入口 |
| --- | --- | --- |
| 文本规范化、Levenshtein 操作数 | `domain/text.py` | `services/evaluation.py`、`services/benchmark.py` 继续 re-export |
| 时间区间分组 | `domain/intervals.py` | `exporters._group_segments` 继续 re-export |
| 绝对时间、时区和偏移格式 | `domain/time.py` | `exporters` 中原私有名称继续 re-export |
| canonical JSON 和 SHA-256 | `domain/hashing.py` | 各版本 service 的 `_sha256_json` / `_sha256_mapping` 名称继续可用 |
| embedding L2 与语音响度归一化 | `audio/embeddings.py` | `services/enrollment.py` 继续 re-export |
| session 原始输入完整性和映射连续性 | `application/use_cases/session_integrity.py` | `services/session_readiness.py` 继续 re-export |

生产调用方已迁移到新入口。兼容 re-export 暂不发出运行时 warning，避免污染 CLI
和长流程日志；删除前必须再次全仓搜索，并确认没有包外调用方依赖旧入口。

## 2. 依赖变化

原来的依赖问题：

```text
quality_asr -> benchmark/evaluation
timeline -> exporters._private
verification/voice_library/v2d3 -> enrollment helpers
session_backup <-> session_readiness
```

现在变为：

```text
quality_asr/benchmark/evaluation -> domain.text
timeline/exporters -> domain.intervals + domain.time
enrollment/verification/voice_library/v2d3 -> audio.embeddings
session_backup -> application.session_integrity
session_readiness -> session_backup + application.session_integrity
```

`services/` 当前没有跨模块私有名称导入、service import cycle 或函数内部的
service import。阶段 0 的三个架构白名单已清空，测试会拒绝这些债务重新出现。

## 3. 哈希边界说明

各 pipeline version 仍各自构造并拥有 request、config、evidence 和 summary
payload；本阶段没有合并字段或协议。提取的只有所有实现原本相同的 JSON 字节规则：

- UTF-8；
- `ensure_ascii=False`；
- key 排序；
- 紧凑的 `,`/`:` 分隔符；
- 对这些字节计算 SHA-256。

字节级 characterization test 固定了包含 Unicode、布尔值、数组和乱序 key 的
canonical 输出。数据库内部证据触发器和具有专用格式的 benchmark hash 没有被
顺便改写。

## 4. 测试

新增 `tests/test_shared_primitives.py`，直接覆盖：

- NFKC/标点/空白规范化和编辑操作分解；
- `max_gap_ms` 等于边界时仍归为同组；
- 时区转换、无效时间和偏移截断；
- zero embedding、float32 输出和 0.95 peak ceiling；
- session gap/overlap 计算；
- canonical JSON 的精确字节和 SHA-256。

最终验证：

```text
python -m unittest discover -v  -> 92 tests passed in 19.916s
ruff check src tests            -> All checks passed
```

测试没有加载真实模型，也没有读取 `data/` 中的真实录音。

## 5. 后续

下一阶段按指南进入 SQLite 基础设施拆分：先提取 connection、migration runner
和历史 migration，保留 `Database` 兼容门面；在开始移动 SQL 前先补空库、历史
升级、回滚和 schema 元数据 characterization tests。
