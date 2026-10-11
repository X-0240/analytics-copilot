"""验证层 v1：只做**机械可判**的三类检查（不做 LLM judge）。

V1 数值一致性：结论里的每个数字，要么在声明来源的步骤结果里能找到，要么判不支持；
V2 证据覆盖：结论里点名的维度值，必须出现在它引用的步骤结果里；
V3 结果充分性：归因类问题只有单维度/无时间序列，或某步返回 0 行 →
  不作因果断言，只降置信度并加 caveat（**不删结论**，避免误杀现象描述）。

设计约束：验证器只消费「结构化结论 + 已执行结果」，不认业务关键词分支，
因此不会退化成"按 revenue/customer 写 if-else"的规则引擎。
"""

import re
from dataclasses import dataclass, field

NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|％)?")
# 这些数字不是"数据断言"，检查前先剔掉，否则会误拦（M2 实测到的假阳性）：
#   1. 日期：2010-12 / 2011年12月 / 2011/01
#   2. 标识：商品 23843 / SKU 85123A / 编号 12345
#   3. 排名：前 10 名 / 第 3 位
DATE_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}(\s*[-/月]\s*\d{1,2})?")
IDENT_RE = re.compile(
    r"(商品|SKU|sku|货号|编号|客户|invoice)\s*[:：]?\s*[A-Za-z0-9\-]{3,}"
)
RANK_RE = re.compile(r"(前|第)\s*\d+")
ATTRIBUTION_WORDS = ("为什么", "原因", "导致", "归因", "贡献")
# 维度值中英对照（数据里是国家英文名，结论常用中文）：只覆盖常用国家；
# 覆盖不到会判 V2 失败，这是已知限制，不假装通用。
DIMENSION_ALIASES = {
    "英国": "united kingdom",
    "德国": "germany",
    "法国": "france",
    "荷兰": "netherlands",
    "比利时": "belgium",
    "瑞士": "switzerland",
    "西班牙": "spain",
    "葡萄牙": "portugal",
    "意大利": "italy",
    "瑞典": "sweden",
    "澳大利亚": "australia",
}


@dataclass
class Violation:
    """一条违反：类型 + 说明 + 相关结论下标。"""

    kind: str
    detail: str
    claim_index: int = -1


@dataclass
class Verdict:
    """验证结果：逐条结论的判定 + 汇总。"""

    claim_status: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    status: str = "supported"
    caveats: list = field(default_factory=list)

    def blocked(self):
        """被 V1/V2 拦下的违反（这些会让结论失去证据支持）。"""
        return [item for item in self.violations if item.kind in ("V1", "V2")]

    def downgraded(self):
        """V3 只降置信度，不算拦截。"""
        return [item for item in self.violations if item.kind == "V3"]


def _cells(executed):
    """把已执行结果摊平成 (step_index, 单元格值)，便于数值与字符串匹配。"""
    values = []
    for item in executed:
        for row in item.get("rows") or []:
            for cell in row:
                values.append((item["index"], cell))
    return values


def _close(left, right):
    """数值比较：绝对容差 0.01，或相对 2%（覆盖四舍五入与百分比换算）。"""
    if left is None or right is None:
        return False
    try:
        first, second = float(left), float(right)
    except (TypeError, ValueError):
        return False
    if abs(first - second) <= 0.01:
        return True
    return abs(first - second) <= 0.02 * max(abs(first), abs(second), 1e-9)


def _number_in_steps(value, step_index, executed):
    """声明值是否出现在指定步骤（或整体）的结果里。"""
    for index, cell in _cells(executed):
        if step_index is not None and index != step_index:
            continue
        if _close(value, cell):
            return True
    return False


def verify_claims(claims, executed):
    """对结构化结论逐条跑 V1/V2；返回 (逐条状态, violations)。"""
    statuses, violations = [], []
    lowered_cells = [
        str(cell).lower() for _index, cell in _cells(executed) if cell is not None
    ]
    for position, claim in enumerate(claims):
        text = str(claim.get("text") or "")
        scannable = _strip_non_claims(text)
        numbers = claim.get("numbers") or []
        dims = claim.get("dimension_values") or []
        declared = [item.get("value") for item in numbers]
        problems = []
        # V1a：结论文本里出现的数字必须被声明来源；V1b：声明值要能在结果里找到
        for match in NUMBER_RE.finditer(scannable):
            raw, mark = match.group(1), match.group(2)
            value = float(raw) / 100 if mark else float(raw)
            if not any(_close(value, item) for item in declared):
                problems.append(("V1", "数字 %s%s 没有声明来源" % (raw, mark or "")))
                continue
            hit = any(
                _close(value, item.get("value"))
                and _number_in_steps(item.get("value"), item.get("step"), executed)
                for item in numbers
            )
            if not hit:
                problems.append(
                    ("V1", "数字 %s%s 与声明的来源步骤对不上" % (raw, mark or ""))
                )
        for item in numbers:
            if not _number_in_steps(item.get("value"), None, executed):
                problems.append(
                    ("V1", "声明的数值 %s 在任何结果里都找不到" % item.get("value"))
                )
        # V2：结论点名的维度值必须真的出现在结果里
        for dim in dims:
            token = str(dim).strip().lower()
            english = DIMENSION_ALIASES.get(str(dim).strip(), "")
            found = token in lowered_cells or (
                english and any(english in cell for cell in lowered_cells)
            )
            if not found:
                problems.append(("V2", "维度值「%s」不在任何结果里" % dim))
        for kind, detail in problems:
            violations.append(Violation(kind, detail, position))
        statuses.append(
            {
                "index": position,
                "text": text,
                "supported": not problems,
                "problems": [detail for _kind, detail in problems],
            }
        )
    return statuses, violations


def _strip_non_claims(text):
    """把日期、标识、排名里的数字剔掉，只留下"数据断言"里的数字。"""
    cleaned = DATE_RE.sub(" ", text)
    cleaned = IDENT_RE.sub(" ", cleaned)
    return RANK_RE.sub(" ", cleaned)


def verify_sufficiency(question, plan, executed):
    """V3：证据够不够支撑"归因"这类强结论——只降级，不拦截。"""
    caveats, violations = [], []
    dims = set()
    has_time_series = False
    for step in (plan or {}).get("steps") or []:
        sql = str(step.get("sql", "")).lower()
        for candidate in ("country", "stock_code", "customer_id"):
            if candidate in sql:
                dims.add(candidate)
        if "date_trunc" in sql:
            has_time_series = True
    asks_attribution = any(word in question for word in ATTRIBUTION_WORDS)
    if asks_attribution and len(dims) < 2 and not has_time_series:
        detail = (
            "问题在问原因，但证据只有 %d 个维度且无时间序列：只能说明现象，"
            "不能支持因果归因" % len(dims)
        )
        caveats.append(detail)
        violations.append(Violation("V3", detail))
    empty = [item["index"] for item in executed if not item.get("row_count")]
    if empty:
        detail = "步骤 %s 返回 0 行，不能作为证据" % empty
        caveats.append(detail)
        violations.append(Violation("V3", detail))
    return violations, caveats


def verify(question, plan, executed, claims):
    """跑完三类检查，给出总体状态与要附加的 caveat。"""
    statuses, violations = verify_claims(claims, executed)
    extra, caveats = verify_sufficiency(question, plan, executed)
    violations.extend(extra)
    blocked = [item for item in violations if item.kind in ("V1", "V2")]
    all_blocked = blocked and all(not item["supported"] for item in statuses)
    if not claims or all_blocked:
        status = "unsupported"
    elif blocked or violations:
        status = "partially_supported"
    else:
        status = "supported"
    if status == "unsupported":
        caveats.append(
            "所有结论都拿不到证据支持：不输出强结论，只报证据与不确定性"
        )
    return Verdict(
        claim_status=statuses,
        violations=violations,
        status=status,
        caveats=caveats,
    )
