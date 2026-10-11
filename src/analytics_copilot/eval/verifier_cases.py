"""验证能力实验：在**固定结论集**上评估验证器本身。

为什么要固定结论集：M2 的两臂是两次不同的模型运行，支持率差异混着噪声，
无法归因。这里把「结论 + 来源步骤 + 期望判定」全部固定并标注（构造人=作者），
于是可以把验证器当成被测对象，直接算漏放率与误拦率。

用法：
    python -m analytics_copilot.eval.verifier_cases --cases <json> [--out <dir>]
"""

import argparse
import json
import sys
from pathlib import Path

from analytics_copilot import verifier


def to_executed(executed):
    """把 {'1': [v, v, ...]} 还原成验证器要的结构。"""
    return [
        {"index": int(index), "rows": [[value] for value in values],
         "row_count": len(values)}
        for index, values in (executed or {}).items()
    ]


def run_case(case):
    """跑一条：逐 claim 比对期望，并记录 V3 是否触发。"""
    executed = to_executed(case.get("executed"))
    verdict = verifier.verify(
        case["question"], case.get("plan") or {}, executed, case["claims"]
    )
    expected = case["expected"]
    got = [item["supported"] for item in verdict.claim_status]
    want = list(expected["supported"])
    v3_fires = bool(verdict.downgraded())
    return {
        "case_id": case["case_id"],
        "purpose": case.get("purpose", ""),
        "claims": len(want),
        "got": got,
        "want": want,
        "v3_fires": v3_fires,
        "v3_expected": bool(expected.get("v3_fires")),
        "v3_correct": v3_fires == bool(expected.get("v3_fires")),
        "status": verdict.status,
    }


def summarize(rows):
    """混淆矩阵口径：不支持=正例（要被抓出来）。

    - 命中 TP：期望不支持且判不支持；
    - **漏放 FN**：期望不支持但判成支持（验证器没看出来）；
    - **误拦 FP**：期望支持但判不支持（验证器冤枉了正确结论）；
    - 正确否定 TN：期望支持且判支持。
    """
    tp = fn = fp = tn = 0
    for row in rows:
        for got, want in zip(row["got"], row["want"]):
            if not want and not got:
                tp += 1
            elif not want and got:
                fn += 1
            elif want and not got:
                fp += 1
            else:
                tn += 1
    total = tp + fn + fp + tn
    v3_cases = [row for row in rows if row["v3_expected"] or row["v3_fires"]]
    return {
        "cases": len(rows),
        "claims": total,
        "unsupported_expected": tp + fn,
        "detected": tp,
        "missed_unsupported_rate": round(fn / (tp + fn), 4) if tp + fn else 0.0,
        "false_block_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0,
        "claim_support_rate": round((tp + tn) / total, 4) if total else 0.0,
        "v3_cases": len(v3_cases),
        "v3_correct": len([row for row in v3_cases if row["v3_correct"]]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eval.verifier_cases")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    rows = [run_case(case) for case in payload["cases"]]
    summary = summarize(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row in rows:
        if row["got"] != row["want"] or not row["v3_correct"]:
            print(
                "  偏差 %s：期望 %s 实得 %s（V3 期望 %s 实得 %s）"
                % (row["case_id"], row["want"], row["got"],
                   row["v3_expected"], row["v3_fires"])
            )
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "results.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
            encoding="utf-8",
        )
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
