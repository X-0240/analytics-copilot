"""时间窗解析：把"最近三个月"这类表述解析成**一个**可复用的时间解释对象。

为什么单独一层：SQL 用什么范围、答案写什么口径、caveat 为什么出现，必须来自
同一个对象，否则会出现"SQL 用 A、答案说 B、metadata 又是 C"。

负责人冻结的三条约束：

1. **锚点是数据集最大时间戳**（`metadata.profile.ts_max`），不是今天——
   这份数据是 2010-12 ~ 2011-12 的历史快照，按今天算会查空；
2. **只有明确的"N 个月/周/天"才解析成区间**；只有"最近"两个字时**不猜**
   （`applied=False`，口径按全期如实声明）。否则「最近哪个国家销售额下降最多」
   这类对比题会被误过滤成一段短窗口，答案直接错；
3. **区间按自然月取**：最近 N 个月 = 含锚点所在月在内的 N 个自然月，
   起点为该区间首月 1 日；末月不完整（锚点日号 < 28）时 `complete_period=False`。
"""

import calendar
import re
from datetime import date, datetime, timedelta

# "最近三个月 / 近 3 个月 / 最近两星期 / 最近 7 天 / 近1月"
WINDOW_RE = re.compile(
    r"(最近|近)\s*([0-9]{1,3}|[一二三四五六七八九十两]+)\s*个?\s*(个月|月|周|星期|天|日)"
)

MONTH_WORDS = {"个月", "月"}
WEEK_WORDS = {"周", "星期"}
DAY_WORDS = {"天", "日"}

CN_DIGITS = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _to_int(raw):
    """把 '3' / '三' / '十二' 这类写法转成整数；转不了返回 None。"""
    text = str(raw).strip()
    if text.isdigit():
        return int(text)
    if not text:
        return None
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + CN_DIGITS.get(text[1:], 0)
    if text.endswith("十"):
        return CN_DIGITS.get(text[0], 0) * 10
    if "十" in text:
        head, _, tail = text.partition("十")
        return CN_DIGITS.get(head, 0) * 10 + CN_DIGITS.get(tail, 0)
    if len(text) == 1:
        return CN_DIGITS.get(text)
    return None


def requested_window(text):
    """从问题里取出可解析的时间窗短语（没有则返回空串）。"""
    match = WINDOW_RE.search(str(text or ""))
    return match.group(0).replace(" ", "") if match else ""


def _unit_of(unit_text):
    if unit_text in MONTH_WORDS:
        return "month"
    if unit_text in WEEK_WORDS:
        return "week"
    if unit_text in DAY_WORDS:
        return "day"
    return None


def _month_start(anchor, months_back):
    """锚点所在月往前推 months_back 个自然月的 1 日。"""
    index = anchor.year * 12 + (anchor.month - 1) - months_back
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def resolve(text, profile):
    """解析时间窗；返回时间解释对象或 None（无明确窗口）。

    返回结构（**唯一事实来源**）：

    ```python
    {
      "requested": "最近三个月",
      "anchor": "dataset_max_date",
      "anchor_value": "2011-12-09 12:50:00",
      "unit": "month",
      "n": 3,
      "resolved_range": ["2011-10-01", "2011-12-09"],
      "complete_period": False,
      "applied": True,
    }
    ```
    """
    phrase = requested_window(text)
    if not phrase:
        return None
    match = WINDOW_RE.search(phrase)
    if not match:
        return None
    number = _to_int(match.group(2))
    unit = _unit_of(match.group(3))
    if not number or number <= 0 or unit is None:
        return None
    anchor = _anchor(profile)
    if anchor is None:
        return None
    anchor_day = anchor.date()
    if unit == "month":
        start = _month_start(anchor_day, number - 1)
        # 末月只统计到锚点那天，日号 < 28 视为不完整月
        complete = anchor_day.day >= 28
    elif unit == "week":
        start = anchor_day - timedelta(days=7 * number - 1)
        complete = True
    else:
        start = anchor_day - timedelta(days=number - 1)
        complete = True
    return {
        "requested": phrase,
        "anchor": "dataset_max_date",
        "anchor_value": str(profile.get("ts_max") or ""),
        "unit": unit,
        "n": number,
        "resolved_range": [start.isoformat(), anchor_day.isoformat()],
        "end_timestamp": anchor.strftime("%Y-%m-%d %H:%M:%S"),
        "complete_period": complete,
        "applied": True,
    }


def _anchor(profile):
    raw = str((profile or {}).get("ts_max") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def full_period(profile):
    """没有可用时间窗时的口径对象（全期）。"""
    start = str((profile or {}).get("ts_min") or "")[:10]
    end = str((profile or {}).get("ts_max") or "")[:10]
    return {
        "requested": "",
        "anchor": "dataset_max_date",
        "anchor_value": str((profile or {}).get("ts_max") or ""),
        "unit": "",
        "n": 0,
        "resolved_range": [start, end],
        "end_timestamp": str((profile or {}).get("ts_max") or ""),
        "complete_period": False,
        "applied": False,
    }


def where_clause(window):
    """给 SQL 用的过滤片段；未生效时返回空串。"""
    if not window or not window.get("applied"):
        return ""
    start, _end = window["resolved_range"]
    return (
        "invoice_ts >= DATE '%s' AND invoice_ts <= TIMESTAMP '%s'"
        % (start, window["end_timestamp"])
    )


def describe(window):
    """给答案用的口径文案（SQL 与文案读的是同一个对象）。"""
    if not window:
        return "本次时间范围：未解析"
    start, end = window["resolved_range"]
    if not window.get("applied"):
        return (
            "本次时间范围：全期 %s ~ %s（问题没有给出可解析的时间窗，"
            "如“最近三个月”会被解析并生效）" % (start, end)
        )
    tail = "" if window.get("complete_period") else "，末月不完整（只统计到锚点当天）"
    return (
        "本次时间范围：%s，按数据集最后一天 %s 为锚点解析为 %s ~ %s%s"
        % (
            window["requested"],
            window["anchor_value"][:10],
            start,
            end,
            tail,
        )
    )


def month_span(start_text, end_text):
    """区间内包含的自然月数（只用于文案）。"""
    try:
        first = date.fromisoformat(start_text)
        last = date.fromisoformat(end_text)
    except (TypeError, ValueError):
        return 0
    return (last.year - first.year) * 12 + (last.month - first.month) + 1


def month_end(year, month):
    """某月最后一天（供需要的调用方使用）。"""
    return calendar.monthrange(year, month)[1]
