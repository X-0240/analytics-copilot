"""holdout 回归：把冻结的 holdout 集跑一遍当前验证层，量漏放/误拦。

为什么要有这个脚本：M4-A 的 holdout 数字当时是用临时脚本跑出来的，
临时脚本删掉后**无法复现**。规则改版（v1.2）必须能用同一把尺子重测，
所以把它固化成仓库脚本。

用法：
    python scripts/holdout_eval.py --file eval/holdout/holdout_v1.json \
        --out eval/runs/holdout_v1_v12
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analytics_copilot import verifier, versions  # noqa: E402


def _is_numeric_like(text):
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _columns_for(row):
    """还原列名：首格是字符串且不是数字时视为维度值列，其余按数值列处理。

    这一步是为了让 v1.2 的"实体字段列"规则能在 holdout 上生效——
    集里只存了单元格，没有列名。
    """
    columns = []
    for position, cell in enumerate(row):
        if position == 0 and isinstance(cell, str) and not _is_numeric_like(cell):
            columns.append("dimension_value")
        else:
            columns.append("value")
    return columns


def _executed_for(case):
    steps = []
    for key in sorted(case.get("executed") or {}, key=lambda item: int(item)):
        row = list(case["executed"][key])
        steps.append(
            {
                "index": int(key),
                "name": "holdout-step-%s" % key,
                "sql": "",
                "columns": _columns_for(row),
                # 空数组表示"这一步 0 行"（V3 的触发条件之一），不能当成 1 行
                "rows": [row] if row else [],
                "row_count": 1 if row else 0,
            }
        )
    return steps


def evaluate(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    results = []
    for case in payload["cases"]:
        executed = _executed_for(case)
        statuses, violations = verifier.verify_claims(case["claims"], executed)
        expected = (case.get("expected") or {}).get("supported") or []
        got = [item["supported"] for item in statuses]
        expected_v3 = bool((case.get("expected") or {}).get("v3_fires"))
        _extra, _caveats = verifier.verify_sufficiency(
            case.get("question", ""), case.get("plan") or {}, executed
        )
        got_v3 = any(item.kind == "V3" for item in _extra)
        missed = [i for i, (want, actual) in enumerate(zip(expected, got))
                  if not want and actual]
        wrong = [i for i, (want, actual) in enumerate(zip(expected, got))
                 if want and not actual]
        results.append(
            {
                "case_id": case["case_id"],
                "purpose": case.get("purpose", ""),
                "hard_note": case.get("hard_note", ""),
                "expected": expected,
                "got": got,
                "missed": missed,
                "wrong": wrong,
                "v3_expected": expected_v3,
                "v3_got": got_v3,
                "violations": [
                    {"kind": item.kind, "detail": item.detail,
                     "claim": item.claim_index}
                    for item in violations
                ],
            }
        )
    expected_unsupported = sum(
        1 for item in results for flag in item["expected"] if not flag
    )
    expected_supported = sum(
        1 for item in results for flag in item["expected"] if flag
    )
    missed = sum(len(item["missed"]) for item in results)
    wrong = sum(len(item["wrong"]) for item in results)
    return {
        "file": Path(path).name,
        "version": payload.get("version"),
        "versions": versions.stamp(),
        "cases": len(results),
        "claims": expected_unsupported + expected_supported,
        "expected_unsupported": expected_unsupported,
        "caught": expected_unsupported - missed,
        "miss_rate": round(missed / expected_unsupported, 4)
        if expected_unsupported
        else None,
        "false_reject_rate": round(wrong / expected_supported, 4)
        if expected_supported
        else None,
        "false_rejects": [
            {"case_id": item["case_id"], "purpose": item["purpose"]}
            for item in results
            if item["wrong"]
        ],
        "v3_expected": sum(1 for item in results if item["v3_expected"]),
        "v3_hit": sum(
            1 for item in results if item["v3_expected"] and item["v3_got"]
        ),
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="holdout 回归")
    parser.add_argument("--file", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    summary = evaluate(args.file)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
