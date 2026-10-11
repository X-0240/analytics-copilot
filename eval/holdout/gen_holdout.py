"""机械生成 holdout 验证集：**期望判定由生成规则决定**，不靠人工事后标注。

这是 M4 外部有效性的核心：M3 的固定集是我自己写题干、自己标期望，
存在"自出题自评分"的风险；这里改成——

1. 从真实库里取真实结果行（国家占比、商品退货额、月度退货量）；
2. 由生成规则合成结论：
   - `supported`：数字直接取自结果行，维度值也取自结果行；
   - `unsupported_number`：文本里的数字被扰动（×1.15），声明值仍是真值；
   - `unsupported_value`：声明一个结果里根本不存在的值；
   - `unsupported_dimension`：点名一个结果里不存在的维度值；
   - `boundary_identifier`：文本含日期/编号/排名，但数据断言部分真实 → 应支持；
   - `v3_attribution`：归因问题 + 单维度计划 → V3 应触发（结论本身仍支持）。
3. 每条都写出"期望判定"，**该判定来自规则而非人工判断**。

用法：
    python eval/holdout/gen_holdout.py --out eval/holdout/holdout_v1.json
"""

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "src"))

from analytics_copilot import db  # noqa: E402

SEED = 20261011


def sample_rows():
    """从真实库取三类结果行（每条含维度值与数值）。"""
    con = db.connect()
    try:
        countries = con.execute(
            """
            SELECT country, ROUND(SUM(line_revenue), 2) AS revenue,
                   ROUND(SUM(line_revenue) / (SELECT SUM(line_revenue) FROM v_sales), 4)
                   AS share
            FROM v_sales GROUP BY 1 ORDER BY revenue DESC LIMIT 8
            """
        ).fetchall()
        products = con.execute(
            """
            SELECT stock_code, ROUND(SUM(line_revenue), 2) AS amount
            FROM v_returns GROUP BY 1 ORDER BY amount ASC LIMIT 6
            """
        ).fetchall()
        months = con.execute(
            """
            SELECT STRFTIME(DATE_TRUNC('month', invoice_ts), '%Y-%m') AS month,
                   COUNT(*) AS returns
            FROM v_returns GROUP BY 1 ORDER BY month LIMIT 6
            """
        ).fetchall()
        return [list(row) for row in countries], [list(row) for row in products], [
            list(row) for row in months
        ]
    finally:
        con.close()


def build_cases(seed=SEED):
    """按规则合成案例；标签由构造方式决定。"""
    rng = random.Random(seed)
    countries, products, months = sample_rows()
    cases = []
    counter = 0

    def add(purpose, question, plan, executed, claims, supported, v3=False):
        nonlocal counter
        counter += 1
        cases.append(
            {
                "case_id": "HO-%03d" % counter,
                "purpose": purpose,
                "question": question,
                "plan": plan,
                "executed": executed,
                "claims": claims,
                "expected": {"supported": supported, "v3_fires": v3},
            }
        )

    country_plan = {"steps": [{"sql": "SELECT country, revenue, share FROM v_sales GROUP BY 1"}]}
    product_plan = {"steps": [{"sql": "SELECT stock_code, amount FROM v_returns GROUP BY 1"}]}
    month_plan = {
        "steps": [
            {
                "sql": "SELECT DATE_TRUNC('month', invoice_ts) AS m, COUNT(*) AS n "
                "FROM v_returns GROUP BY 1"
            }
        ]
    }

    for name, revenue, share in countries:
        rows = [name, revenue, share]
        # 1 支持：数字与维度都取自结果
        add(
            "数字与维度都来自结果",
            "各国销售额占比",
            country_plan,
            {"1": rows},
            [
                {
                    "text": "%s 的销售额为 %s，占比 %s%%" % (name, revenue, share * 100),
                    "numbers": [
                        {"value": revenue, "step": 1},
                        {"value": share, "step": 1},
                    ],
                    "dimension_values": [name],
                }
            ],
            [True],
        )
        # 2 不支持：文本数字被扰动（声明值仍是真值）
        inflated = round(revenue * 1.15, 2)
        add(
            "文本数字被扰动，与声明来源不符",
            "各国销售额",
            country_plan,
            {"1": rows},
            [
                {
                    "text": "%s 的销售额为 %s（声明来源为真实值）" % (name, inflated),
                    "numbers": [{"value": revenue, "step": 1}],
                    "dimension_values": [name],
                }
            ],
            [False],
        )
        # 3 不支持：声明一个结果里不存在的值
        add(
            "声明的数值在结果里不存在",
            "各国销售额",
            country_plan,
            {"1": rows},
            [
                {
                    "text": "%s 的销售额约 %s" % (name, round(revenue * 7.77, 2)),
                    "numbers": [{"value": round(revenue * 7.77, 2), "step": 1}],
                    "dimension_values": [name],
                }
            ],
            [False],
        )
        # 4 边界：文本含非数据断言数字（前 5 名 / 编号 / 日期）
        add(
            "含排名与编号等非数据断言数字，应仍判支持",
            "各国销售额前十",
            country_plan,
            {"1": rows},
            [
                {
                    "text": "前 5 名里 %s（编号 C-001）销售额为 %s" % (name, revenue),
                    "numbers": [{"value": revenue, "step": 1}],
                    "dimension_values": [name],
                }
            ],
            [True],
        )

    for code, amount in products:
        rows = [code, amount]
        magnitude = abs(amount)
        # 5 支持：退货金额用绝对值表述（数据里为负）
        add(
            "退货金额以绝对值表述，应判支持（有意放宽）",
            "退货金额最高的商品",
            product_plan,
            {"1": rows},
            [
                {
                    "text": "商品 %s 的退货金额约 %s" % (code, magnitude),
                    "numbers": [{"value": magnitude, "step": 1}],
                    "dimension_values": [],
                }
            ],
            [True],
        )
        # 6 不支持：维度值不在结果里
        add(
            "点名的维度值不在结果里",
            "退货金额最高的商品",
            product_plan,
            {"1": rows},
            [
                {
                    "text": "商品 UNKNOWN-9999 的退货金额最高，约 %s" % magnitude,
                    "numbers": [{"value": magnitude, "step": 1}],
                    "dimension_values": ["UNKNOWN-9999"],
                }
            ],
            [False],
        )

    for month, count in months:
        rows = [month + "-01 00:00:00", count]
        # 7 支持：日期数字不算数据断言
        add(
            "日期数字不得被当成数据断言",
            "月度退货量",
            month_plan,
            {"1": rows},
            [
                {
                    "text": "%s 的退货量为 %d 件" % (month, count),
                    "numbers": [{"value": count, "step": 1}],
                    "dimension_values": [],
                }
            ],
            [True],
        )
        # 8 不支持：数字对但声明的步骤不存在
        add(
            "声明的来源步骤不存在",
            "月度退货量",
            month_plan,
            {"1": rows},
            [
                {
                    "text": "%s 的退货量为 %d 件" % (month, count),
                    "numbers": [{"value": count, "step": 9}],
                    "dimension_values": [],
                }
            ],
            [False],
        )

    # 9 V3：归因问题 + 单维度计划（结论本身支持，但证据不足以谈原因）
    add(
        "归因问题 + 单维度 → V3 应触发",
        "为什么销售额下降",
        country_plan,
        {"1": countries[0]},
        [
            {
                "text": "%s 的销售额为 %s，占比 %s%%"
                % (countries[0][0], countries[0][1], countries[0][2] * 100),
                "numbers": [
                    {"value": countries[0][1], "step": 1},
                    {"value": countries[0][2], "step": 1},
                ],
                "dimension_values": [countries[0][0]],
            }
        ],
        [True],
        v3=True,
    )
    # 10 V3：某步 0 行
    add(
        "存在 0 行步骤 → V3 应触发",
        "各国销售额",
        country_plan,
        {"1": [], "2": countries[0]},
        [
            {
                "text": "%s 占比 %s%%" % (countries[0][0], countries[0][2] * 100),
                "numbers": [{"value": countries[0][2], "step": 2}],
                "dimension_values": [countries[0][0]],
            }
        ],
        [True],
        v3=True,
    )
    rng.shuffle(cases)
    return cases


def build_hard_cases():
    """困难 holdout：按**业务意图**标注，专门去撞规则的已知边界。

    期望标签不是"规则会怎么判"，而是"业务上这句话是否被数据支持"；
    因此如果验证器在别名表外国家、单位换算等情形误拦，会如实显示成误拦率。
    """
    countries, products, months = sample_rows()
    top_country, top_revenue, top_share = countries[0]
    plan = {"steps": [{"sql": "SELECT country, revenue, share FROM v_sales GROUP BY 1"}]}
    other = countries[3]
    cases = [
        {
            "case_id": "HO-HARD-01",
            "purpose": "扰动 1.5%：在容差内，业务上可接受",
            "question": "各国销售额占比",
            "plan": plan,
            "executed": {"1": [top_country, top_revenue, top_share]},
            "claims": [
                {
                    "text": "%s 占比约 %.1f%%" % (top_country, top_share * 100 * 1.015),
                    "numbers": [{"value": round(top_share * 1.015, 4), "step": 1}],
                    "dimension_values": [],
                }
            ],
            "expected": {"supported": [True], "v3_fires": False},
            "hard_note": "容差内扰动：既可能判支持也可能判不支持，看阈值取向",
        },
        {
            "case_id": "HO-HARD-02",
            "purpose": "扰动 3%：超出容差，应判不支持",
            "question": "各国销售额占比",
            "plan": plan,
            "executed": {"1": [top_country, top_revenue, top_share]},
            "claims": [
                {
                    "text": "%s 占比约 %.1f%%" % (top_country, top_share * 100 * 1.03),
                    "numbers": [{"value": top_share, "step": 1}],
                    "dimension_values": [],
                }
            ],
            "expected": {"supported": [False], "v3_fires": False},
            "hard_note": "同一条结论、两种标注取向，用来检验阈值是否真的在起作用",
        },
        {
            "case_id": "HO-HARD-03",
            "purpose": "中文写法是别名表外的国家（业务上支持，规则可能误拦）",
            "question": "各国销售额占比",
            "plan": plan,
            "executed": {"1": ["South Africa", other[1], other[2]]},
            "claims": [
                {
                    "text": "南非贡献 %s%%" % (other[2] * 100),
                    "numbers": [{"value": other[2], "step": 1}],
                    "dimension_values": ["南非"],
                }
            ],
            "expected": {"supported": [True], "v3_fires": False},
            "hard_note": "别名表只覆盖 12 个常用国家；表外会出现误拦",
        },
        {
            "case_id": "HO-HARD-04",
            "purpose": "金额用「万」表述（业务上支持，机械匹配做不到）",
            "question": "销售额总量",
            "plan": {"steps": [{"sql": "SELECT SUM(line_revenue) FROM v_sales"}]},
            "executed": {"1": [top_revenue]},
            "claims": [
                {
                    "text": "第一名国家的销售额约 %.2f 万元" % (top_revenue / 10000),
                    "numbers": [{"value": round(top_revenue / 10000, 2), "step": 1}],
                    "dimension_values": [],
                }
            ],
            "expected": {"supported": [True], "v3_fires": False},
            "hard_note": "单位换算（万）不被机械匹配支持，属已知限制",
        },
        {
            "case_id": "HO-HARD-05",
            "purpose": "裸数字编号当成数据断言（业务上是编号，机械当数字）",
            "question": "退货金额最高的商品",
            "plan": {"steps": [{"sql": "SELECT stock_code, amount FROM v_returns GROUP BY 1"}]},
            "executed": {"1": [products[0][0], products[0][1]]},
            "claims": [
                {
                    "text": "23843 的退货金额约 %s" % abs(products[0][1]),
                    "numbers": [{"value": abs(products[0][1]), "step": 1}],
                    "dimension_values": [],
                }
            ],
            "expected": {"supported": [True], "v3_fires": False},
            "hard_note": "纯数字编号不带前缀时无法与数字断言区分，属已知限制",
        },
        {
            "case_id": "HO-HARD-06",
            "purpose": "日期变体 2010/12 也应被剥离",
            "question": "月度退货量",
            "plan": {"steps": [{"sql": "SELECT DATE_TRUNC('month', invoice_ts), COUNT(*) FROM v_returns GROUP BY 1"}]},
            "executed": {"1": [months[0][0] + "-01 00:00:00", months[0][1]]},
            "claims": [
                {
                    "text": "2010/12 的退货量为 %d 件" % months[0][1],
                    "numbers": [{"value": months[0][1], "step": 1}],
                    "dimension_values": [],
                }
            ],
            "expected": {"supported": [True], "v3_fires": False},
            "hard_note": "日期写法变体",
        },
        {
            "case_id": "HO-HARD-07",
            "purpose": "归因 + 双维度 → V3 不应触发",
            "question": "为什么销售额下降",
            "plan": {"steps": [
                {"sql": "SELECT country, SUM(line_revenue) FROM v_sales GROUP BY 1"},
                {"sql": "SELECT stock_code, SUM(line_revenue) FROM v_sales GROUP BY 1"},
            ]},
            "executed": {"1": [top_country, top_revenue], "2": ["85123A", 1000.5]},
            "claims": [
                {
                    "text": "%s 销售额 %s" % (top_country, top_revenue),
                    "numbers": [{"value": top_revenue, "step": 1}],
                    "dimension_values": [top_country],
                }
            ],
            "expected": {"supported": [True], "v3_fires": False},
            "hard_note": "与 HO-HARD-08 成对照",
        },
        {
            "case_id": "HO-HARD-08",
            "purpose": "归因 + 单维度 → V3 应触发",
            "question": "为什么销售额下降",
            "plan": {"steps": [{"sql": "SELECT country, SUM(line_revenue) FROM v_sales GROUP BY 1"}]},
            "executed": {"1": [top_country, top_revenue]},
            "claims": [
                {
                    "text": "%s 销售额 %s" % (top_country, top_revenue),
                    "numbers": [{"value": top_revenue, "step": 1}],
                    "dimension_values": [top_country],
                }
            ],
            "expected": {"supported": [True], "v3_fires": True},
            "hard_note": "与 HO-HARD-07 成对照",
        },
    ]
    return cases


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gen_holdout")
    parser.add_argument("--out", default=str(PROJECT / "eval/holdout/holdout_v1.json"))
    parser.add_argument(
        "--mode",
        choices=("clean", "hard"),
        default="clean",
        help="clean=规则覆盖；hard=按业务意图标注的困难边界",
    )
    args = parser.parse_args(argv)
    cases = build_cases() if args.mode == "clean" else build_hard_cases()
    payload = {
        "version": "holdout-v1" if args.mode == "clean" else "holdout-hard-v1",
        "generated_by": "eval/holdout/gen_holdout.py",
        "seed": SEED,
        "note": "期望判定由生成规则决定（非人工标注）；生成后即冻结，不得按结果调整规则",
        "cases": cases,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    Path(args.out).write_text(text, encoding="utf-8")
    print("生成 %d 条 → %s" % (len(cases), args.out))
    print("SHA-256 %s" % hashlib.sha256(text.encode("utf-8")).hexdigest().upper())
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
