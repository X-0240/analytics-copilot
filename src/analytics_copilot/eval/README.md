# 评测骨架（M0 占位，M3 实现）

## 冻结指标（口径以《项目四立项决策稿 v1》为准，这里不重复定义）

| 指标 | 现状 |
|---|---|
| 结论支持率 | **未实现**（需先冻结判定规则与阈值） |
| 追溯链完整率 | 骨架已能算「有证据的结论比例」，正式口径待 M3 |
| 误判率 | **未实现**（需 holdout，阈值在 M3 冻结） |
| 边界降级正确率 | **未实现** |
| 对照实验（无验证层 vs 有验证层） | **未实现**（M4） |

## 纪律

1. 先跑 dev 基线 → **再冻结阈值** → 之后才碰 holdout；
2. 构造 gold 必须标注「构造人 = 作者」，不得写成真实用户需求；
3. 不达标如实记录为限制，不调口径、不删失败样例。

## 任务集格式（草案）

```json
{
  "version": "v0-draft",
  "split": "dev",
  "tasks": [
    {
      "task_id": "T-001",
      "question": "最近三个月销售额为什么下降？",
      "expected": {"type": "attribution", "key_facts": ["..."]},
      "provenance": "synthetic_with_basis#author"
    }
  ]
}
```

## 用法（骨架）

```bash
python -m analytics_copilot.eval.runner --tasks <路径> --out <目录>
```

骨架只统计「能否跑通 + 结论是否带证据」，其余指标在 M3 补。
