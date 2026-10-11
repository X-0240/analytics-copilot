"""工具面：只读 SQL、元数据、贡献度分解，以及两类机械可判的结果检查。

M0 只实现**机械可判**的检查（不依赖 LLM）：
  1. fan-out 重复计数：join 的键在右表不唯一且外层做了聚合 → 判风险；
  2. 占比合计检查：结果里像占比的列，各行相加明显偏离 1 → 判风险。
这两种都是"结论不一定错、但必须先解释"的情形，因此返回风险提示而不是直接报错。
"""

from dataclasses import dataclass, field

from analytics_copilot import db

SHARE_HINTS = ("ratio", "share", "pct", "percent", "占比")


@dataclass
class CheckResult:
    """一次检查的结论：是否通过 + 说明 + 证据。"""

    ok: bool
    name: str
    detail: str


@dataclass
class ToolCall:
    """一条工具调用的可追溯记录（结论的证据链由这些记录拼成）。"""

    tool: str
    input: dict
    ok: bool
    detail: str = ""
    rows: int = 0
    extra: dict = field(default_factory=dict)


def key_uniqueness():
    """各视图中常用连接键的取值占比（1.0 = 唯一），用于 fan-out 判断。"""
    con = db.connect()
    try:
        stats = {}
        for view in db.allowed_views():
            columns = [
                row[0]
                for row in con.execute('DESCRIBE "%s"' % view).fetchall()
            ]
            total = con.execute('SELECT COUNT(*) FROM "%s"' % view).fetchone()[0]
            stats[view] = {"rows": total}
            for column in ("invoice_no", "stock_code", "customer_id", "country"):
                if column not in columns:
                    continue
                distinct = con.execute(
                    'SELECT COUNT(DISTINCT "%s") FROM "%s"' % (column, view)
                ).fetchone()[0]
                stats[view][column] = round(distinct / total, 4) if total else 0.0
        return stats
    finally:
        con.close()


def metadata_tool():
    """返回表结构、口径与白名单——回答"我有什么数据、口径是什么"。"""
    meta = db.load_metadata()
    return ToolCall(
        tool="metadata",
        input={},
        ok=True,
        detail="对象 %s；口径 %d 条" % (
            ", ".join(meta["allowed_views"]),
            len(meta["caliber"]),
        ),
        rows=len(meta["objects"]),
        extra={
            "allowed_views": meta["allowed_views"],
            "caliber": meta["caliber"],
            "profile": meta["profile"],
        },
    )


def check_join_fanout(sql, uniqueness=None):
    """join 键不唯一 + 外层聚合 → 可能重复计数。"""
    uniqueness = uniqueness or key_uniqueness()
    lowered = sql.lower()
    if " join " not in lowered or not any(
        word in lowered for word in ("sum(", "count(", "avg(")
    ):
        return CheckResult(True, "join_fanout", "无 join 或未聚合，跳过")
    risky = []
    for view, stats in uniqueness.items():
        for column in ("invoice_no", "stock_code", "customer_id"):
            ratio = stats.get(column)
            if ratio is None or ratio >= 0.999:
                continue
            if ("join" in lowered and column in lowered) or view in lowered:
                risky.append("%s.%s 唯一率 %.2f" % (view, column, ratio))
    if risky:
        return CheckResult(
            False,
            "join_fanout",
            "join 后聚合，但连接键不唯一：%s → 可能重复计数，需要先确认粒度"
            % "；".join(risky),
        )
    return CheckResult(True, "join_fanout", "连接键唯一性或未涉及聚合")


def check_share_sum(result):
    """结果里像占比的列，合计应接近 1（容差 0.05）。"""
    if result.truncated:
        # 行被截断时占比合计天然不等于 1，不能据此判定口径有问题
        return CheckResult(True, "share_sum", "结果被截断，占比合计不判定")
    for index, column in enumerate(result.columns):
        name = str(column).lower()
        if not any(hint in name for hint in SHARE_HINTS):
            continue
        values = [
            row[index] for row in result.rows if isinstance(row[index], (int, float))
        ]
        if not values:
            continue
        total = sum(values)
        if abs(total - 1.0) > 0.05:
            return CheckResult(
                False,
                "share_sum",
                "列 %s 各行相加为 %.3f，偏离 1 —— 占比口径或分母可疑"
                % (column, total),
            )
    return CheckResult(True, "share_sum", "无占比列或合计接近 1")


def sql_tool(sql, context="分析"):
    """执行一条只读查询，并返回查询与两类检查结果。"""
    result = db.run_sql(sql)
    checks = [check_join_fanout(result.sql)]
    share = check_share_sum(result)
    if not share.ok or share.detail != "无占比列或合计接近 1":
        checks.append(share)
    detail = "返回 %d 行%s" % (
        len(result.rows),
        "（已截断，结论可能不完整）" if result.truncated else "",
    )
    return result, checks, ToolCall(
        tool="sql",
        input={"sql": result.sql, "context": context},
        ok=True,
        detail=detail,
        rows=len(result.rows),
        extra={
            "checks": [
                {"name": item.name, "ok": item.ok, "detail": item.detail}
                for item in checks
            ],
            "truncated": result.truncated,
        },
    )


def decompose_sql(metric="line_revenue", dimension="country", where=""):
    """贡献度分解：按维度看各成员的贡献占比（用于"是否漏掉更大量级因素"）。"""
    template = (
        "SELECT {dim} AS dimension_value, "
        "ROUND(SUM({metric}), 2) AS metric_value, "
        "ROUND(SUM({metric}) / (SELECT SUM({metric}) FROM v_sales {where}), 4) "
        "AS share "
        "FROM v_sales {where} GROUP BY 1 ORDER BY metric_value DESC"
    )
    return template.format(metric=metric, dim=dimension, where=where)
