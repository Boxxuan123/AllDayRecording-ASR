# 精简独立审核入口

基线 ASR e067e72058f5d734a5831567545be43ca521e7b7 / Phone bfbc564d50ef99d895eae8bd324f7edb026d84b6。

本轮请审查：
1. recall_actions.py：联系人目录仅实际来源元数据；owner/query 生命周期；两次有界目录刷新；返回候选才可 choose；时间/人物/来源不能绕过；不写身份。
2. recall_understanding.py：没有实际名字线索的未知联系人直接等待用户选择；模型验证失败原子回退，不能留下半应用的日期/关键词。
3. recall_summary.py / recall_model.py：45 秒归纳、真实故障分类；显式一次重试隔离 batch/job；引用验证、失效、取消；无隐式重试。
4. PhoneRecallSearch / PhoneProductRecall：无候选时可查联系人；选后自动继续；原问题保留；失败有原文/重试按钮；70 秒 HTTP 等待与 60 秒查询预算匹配。
5. tests/test_recall_recovery.py 与 tools/quality/check-recall-query-loop.cjs：最终回答内容、约束、防跨 owner、分页、取消、24 秒模拟模型、选择不写人物关系。

验收只使用虚构数据与模拟模型；本轮没有主动读取真实聊天正文或额外调用真实模型。Mac 备注导出缺口、真实模型最终效果须据实标注，不用工程模拟替代。
