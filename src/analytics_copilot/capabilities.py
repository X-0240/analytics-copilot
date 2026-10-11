"""数据能力卡：把"这份数据能分析什么、不能分析什么"变成 Agent 的一部分。

为什么声明部分写在代码里、而不是手写进 `metadata.json`：
`metadata.json` 是 `scripts/build_db.py` 生成的产物，重建会覆盖手改内容。
这里的做法是——**事实部分从 metadata 现读**（时间范围、行数、视图列），
**不能做什么 + 为什么**写在代码里，`card()` 把两者合并。这样能力卡既不会与
数据漂移，也不会被重建冲掉。

用途限定两条（不扩成大而全的能力推理层）：

1. **入口展示**：`GET /capabilities` + 首页第一屏，用户先知道能问什么；
2. **拒答依据**：命中不支持的能力时，拒答理由直接引用能力卡，而不是一句"超出边界"。
"""

from analytics_copilot import db

# 不支持的能力：词表用于机械判定，reason 会出现在拒答理由里
UNSUPPORTED = (
    {
        "key": "profit",
        "label": "成本 / 利润",
        "words": ("利润", "毛利", "成本", "费用"),
        "reason": "只读视图里没有成本或利润字段，算不出利润",
    },
    {
        "key": "forecast",
        "label": "预测",
        "words": ("预测", "预计", "预估", "未来"),
        "reason": "只做历史数据的探索与比较，不做预测",
    },
    {
        "key": "inventory",
        "label": "库存 / 补货",
        "words": ("库存", "缺货", "补货", "备货"),
        "reason": "数据里没有库存字段",
    },
    {
        "key": "external",
        "label": "外部市场 / 竞品 / 投放",
        "words": ("竞品", "竞争对手", "广告", "投放", "市场份额", "促销活动"),
        "reason": "数据里没有外部市场、竞品或投放字段",
    },
)

# 能做什么：与列相关的那部分在 card() 里按实际视图派生
SUPPORTED_ANALYSES = (
    "销售趋势（按月）",
    "维度对比与拆解（国家 / 商品 / 客户）",
    "退货分析",
    "异常月份与集中度定位",
)

CAVEAT = "只给证据支持的相关性描述，不声明因果；问题涉及不支持的能力时先说明限制。"


def _columns(meta):
    return [
        item["column"]
        for item in (meta.get("columns") or {}).get("v_sales") or []
    ]


def card(meta=None):
    """返回能力卡（事实来自 metadata，声明来自本模块）。"""
    meta = meta if meta is not None else db.load_metadata()
    profile = meta.get("profile") or {}
    columns = _columns(meta)
    metrics = [
        label for label, column in (("销售额", "line_revenue"), ("数量", "quantity"))
        if column in columns
    ]
    if "is_return" in columns:
        metrics.append("退货")
    dimensions = [
        label
        for label, column in (
            ("国家", "country"),
            ("商品", "stock_code"),
            ("客户", "customer_id"),
            ("时间", "invoice_ts"),
        )
        if column in columns
    ]
    return {
        "domain": "在线零售交易（UCI Online Retail Demo）",
        "source_note": "公开数据集，非真实企业数据；只有交易明细，没有成本/库存/市场字段",
        "rows": profile.get("rows"),
        "grain": (meta.get("caliber") or {}).get("grain", ""),
        "time_range": [
            str(profile.get("ts_min") or "")[:10],
            str(profile.get("ts_max") or "")[:10],
        ],
        "metrics": metrics,
        "dimensions": dimensions,
        "supported": list(SUPPORTED_ANALYSES),
        "unsupported": [
            {"label": item["label"], "reason": item["reason"]}
            for item in UNSUPPORTED
        ],
        "caveat": CAVEAT,
    }


def refusal_for(question):
    """命中不支持的能力时返回 (label, reason)，否则 None。"""
    text = str(question or "")
    for item in UNSUPPORTED:
        if any(word in text for word in item["words"]):
            return item["label"], item["reason"]
    return None


def capability_words():
    """所有"不支持"的词（供 planner 复用，避免两处各写一份词表）。"""
    return tuple(word for item in UNSUPPORTED for word in item["words"])


def summary_line(meta=None):
    """一句话能力摘要（拒答理由里引用它，让用户知道现在能问什么）。"""
    data = card(meta)
    return "当前可分析：%s × %s（数据 %s ~ %s）" % (
        " / ".join(data["metrics"]),
        " / ".join(data["dimensions"]),
        data["time_range"][0],
        data["time_range"][1],
    )
