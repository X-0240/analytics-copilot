"""解释强度模型（验证标准 v1.1）：用证据决定"能说到哪一级"。

按负责人冻结的三级定义：

- **Level 1 事实描述**：单指标/单维度 + 有直接查询证据；
- **Level 2 相关性解释**：至少一个可比较维度 + 时间变化或对照证据，
  输出**必须**包含固定措辞「该结果展示相关变化模式，不能证明因果关系」；
- **Level 3 因果结论**：本项目**禁止输出**；用户提因果假设时只做数据一致性验证，
  不得声明因果成立。

实现范围：等级由"计划 + 已执行结果"**机械算出**；因果措辞由词表**机械检测**。
越界的处理是**降置信度 + 加固定边界措辞**，不改写结论文本。
"""

import re

LEVEL2_CAVEAT = "该结果展示相关变化模式，不能证明因果关系"
LEVEL3_CAVEAT = "本项目不支持因果结论：只能做数据一致性验证，不能声明因果成立"

# 因果措辞（机械可判）：命中即视为试图下因果结论
CAUSAL_PATTERNS = (
    "导致",
    "造成",
    "引起了",
    "引起",
    "因为",
    "由于",
    "根本原因",
    "证明了",
    "源于",
)
CAUSAL_RE = re.compile("|".join(CAUSAL_PATTERNS))
DIMENSION_COLUMNS = ("country", "stock_code", "customer_id")


def evidence_level(plan, executed):
    """算出当前证据支持到哪一级（1 或 2；永不返回 3）。"""
    steps = (plan or {}).get("steps") or []
    sqls = [str(step.get("sql", "")).lower() for step in steps]
    dims = {
        name for name in DIMENSION_COLUMNS if any(name in sql for sql in sqls)
    }
    has_time_series = any("date_trunc" in sql for sql in sqls)
    has_rows = any(item.get("row_count") for item in executed or [])
    if not has_rows:
        return 1
    comparable = bool(dims) and (has_time_series or len(dims) >= 2)
    return 2 if comparable else 1


def has_causal_wording(text):
    """结论里是否出现因果措辞。"""
    return bool(CAUSAL_RE.search(str(text or "")))


# 提问或拒答理由里出现这些词，说明是"因果类提问"：
# 按 v1.1 不允许直接拒答，要改成给到证据允许的等级 + 边界措辞。
CAUSAL_TOPIC_RE = re.compile("因果|原因|为什么|导致|因为")


def is_causal_topic(*texts):
    """是否属于因果类提问或因果类拒答理由。"""
    return any(CAUSAL_TOPIC_RE.search(str(text or "")) for text in texts)


def level_caveats(level):
    """该等级必须附加的边界措辞。"""
    if level >= 2:
        return [LEVEL2_CAVEAT, LEVEL3_CAVEAT]
    return [LEVEL3_CAVEAT]


def check_level(claims, level):
    """逐条检查结论是否超出证据允许的解释等级。

    返回 (越界的 claim 下标列表, 需要附加的 caveat 列表)。
    """
    over = [
        index
        for index, claim in enumerate(claims or [])
        if has_causal_wording(claim.get("text"))
    ]
    caveats = level_caveats(level)
    if over:
        caveats.append(
            "第 %s 条结论使用因果措辞，超出当前证据等级（Level %d），已降级处理"
            % ([index + 1 for index in over], level)
        )
    return over, caveats
