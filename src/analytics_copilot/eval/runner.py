"""评测 runner 骨架：跑一批任务，统计当前**机械可算**的部分。

M0 只算两件不依赖判定规则的事：
  1. 端到端完成率（跑通/澄清/报错各多少）；
  2. 追溯链完整率雏形：结论是否带证据、证据是否带 SQL 与结果行。
其余指标（支持率、误判率、边界降级）需要先冻结判定规则，留给 M3。
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from analytics_copilot import agent


def run_task(task, verify_layer=True):
    """跑单条任务（in-process，不走 HTTP，评测不需要网络）。

    会模拟澄清对话：最多 2 轮澄清，之后按冻结稿规则"带显式假设继续"。
    因此最终状态只可能是 answered / refused / error，
    `clarifications_asked` 单独记录"是否问过"，用于判边界案例行为。
    """
    session = {"clarify_rounds": 0, "history": []}
    asked = 0
    outcome = None
    started = time.perf_counter()
    for _ in range(3):
        try:
            outcome = agent.analyze(
                task["question"], session, verify_layer=verify_layer
            )
            break
        except agent.ClarifyNeeded:
            asked += 1
            continue
        except agent.Refused as exc:
            return {
                "task_id": task["task_id"],
                "kind": task.get("kind"),
                "status": "refused",
                "clarifications_asked": asked,
                "reason": exc.reason,
            }
        except Exception as exc:
            return {
                "task_id": task["task_id"],
                "kind": task.get("kind"),
                "status": "error",
                "error": type(exc).__name__,
            }
    if outcome is None:
        return {
            "task_id": task["task_id"],
            "kind": task.get("kind"),
            "status": "error",
            "error": "CLARIFY_LOOP",
        }
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    conclusion = outcome["conclusion"]
    with_evidence = [
        item
        for item in conclusion.evidence
        if item.get("sql") and item.get("row_count")
    ]
    return {
        "task_id": task["task_id"],
        "kind": task.get("kind"),
        "status": "answered",
        "clarifications_asked": asked,
        "mode": outcome.get("mode", "rule"),
        "claim": conclusion.claim,
        "confidence": conclusion.confidence,
        "caveats": len(conclusion.caveats),
        "evidence_steps": len(conclusion.evidence),
        "steps_with_sql_and_rows": len(with_evidence),
        "trace_complete": len(with_evidence) > 0 and len(with_evidence) == len(
            conclusion.evidence
        ),
        "plan_steps": len((outcome.get("plan") or {}).get("steps", [])),
        "claims_total": len(outcome.get("claims") or []),
        "claims_supported": len(
            [
                item
                for item in (outcome.get("verification") or {}).get(
                    "claim_status", []
                )
                if item.get("supported")
            ]
        ),
        "verify_status": (outcome.get("verification") or {}).get("status", "n/a"),
        "claim_status": (outcome.get("verification") or {}).get(
            "claim_status", []
        ),
        "blocked": (outcome.get("verification") or {}).get("blocked", 0),
        "downgraded": (outcome.get("verification") or {}).get("downgraded", 0),
        "strong_claim": conclusion.confidence in ("high", "medium"),
        "claims": outcome.get("claims") or [],
        "evidence_values": {
            str(item["index"]): [
                cell for row in (item.get("rows") or []) for cell in row
            ][:300]
            for item in outcome.get("executed") or []
        },
        "elapsed_ms": elapsed_ms,
        "cost_cny": outcome.get("cost_cny", 0.0),
    }


def rescore_offline(row):
    """用同一把机械尺子给"未开验证层"的结论打分（离线复评，不改原结果）。"""
    from analytics_copilot import verifier

    evidence = row.get("evidence_values") or {}
    executed = [
        {
            "index": int(index),
            "rows": [[value] for value in values],
            "row_count": len(values),
        }
        for index, values in evidence.items()
    ]
    statuses, violations = verifier.verify_claims(row.get("claims") or [], executed)
    return {
        "claims_total": len(row.get("claims") or []),
        "claims_supported": len([item for item in statuses if item["supported"]]),
        "blocked": len([item for item in violations if item.kind in ("V1", "V2")]),
    }


def compare(run_a, run_b):
    """比对两个 run：A 视为无验证层基线（离线复评），B 为带验证层。"""
    def load(directory):
        path = Path(directory) / "results.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    rows_a, rows_b = load(run_a), load(run_b)
    # 两列都用**当前**验证器离线复评：同一把尺子量两批结论，
    # 运行期用旧规则算出的数字只作行为参考，不进这张对照表。
    scored_a = [rescore_offline(row) for row in rows_a if row["status"] == "answered"]
    scored_b = [rescore_offline(row) for row in rows_b if row["status"] == "answered"]
    claims_a = sum(item["claims_total"] for item in scored_a)
    supported_a = sum(item["claims_supported"] for item in scored_a)
    verified = [
        row for row in rows_b if row["status"] == "answered"
    ]
    claims_b = sum(item["claims_total"] for item in scored_b)
    supported_b = sum(item["claims_supported"] for item in scored_b)
    latency_a = [
        row.get("elapsed_ms") or 0 for row in rows_a if row["status"] == "answered"
    ]
    latency_b = [
        row.get("elapsed_ms") or 0 for row in rows_b if row["status"] == "answered"
    ]
    result = {
        "baseline_run": Path(run_a).name,
        "verified_run": Path(run_b).name,
        "baseline": {
            "claims_total": claims_a,
            "claims_supported": supported_a,
            "claim_support_rate": round(supported_a / claims_a, 4) if claims_a else 0.0,
            "strong_claims": len(
                [row for row in rows_a if row.get("strong_claim")]
            ),
            "cost_cny": round(
                sum(row.get("cost_cny") or 0.0 for row in rows_a), 6
            ),
            "latency_ms_mean": round(sum(latency_a) / len(latency_a))
            if latency_a
            else 0,
        },
        "verified": {
            "claims_total": claims_b,
            "claims_supported": supported_b,
            "claim_support_rate": round(supported_b / claims_b, 4) if claims_b else 0.0,
            "strong_claims": len(
                [row for row in rows_b if row.get("strong_claim")]
            ),
            "blocked": sum(row.get("blocked") or 0 for row in verified),
            "downgraded": sum(row.get("downgraded") or 0 for row in verified),
            "blocked_by_current_rules": sum(item["blocked"] for item in scored_b),
            "unsupported_runs": len(
                [row for row in verified if row.get("verify_status") == "unsupported"]
            ),
            "cost_cny": round(
                sum(row.get("cost_cny") or 0.0 for row in rows_b), 6
            ),
            "latency_ms_mean": round(sum(latency_b) / len(latency_b))
            if latency_b
            else 0,
        },
    }
    result["delta"] = {
        "claim_support_rate": round(
            result["verified"]["claim_support_rate"]
            - result["baseline"]["claim_support_rate"],
            4,
        ),
        "strong_claims": result["verified"]["strong_claims"]
        - result["baseline"]["strong_claims"],
        "latency_ms_mean": result["verified"]["latency_ms_mean"]
        - result["baseline"]["latency_ms_mean"],
        "cost_cny": round(
            result["verified"]["cost_cny"] - result["baseline"]["cost_cny"], 6
        ),
    }
    out_dir = Path(run_b)
    (out_dir / "compare.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 验证层前后对照（%s vs %s）" % (result["baseline_run"], result["verified_run"]),
        "",
        "| 指标 | 无验证层（离线复评） | 有验证层 | 差 |",
        "|---|---|---|---|",
        "| 结论条数 | %d | %d | — |"
        % (claims_a, claims_b),
        "| 被证据支持的结论 | %d | %d | — |"
        % (supported_a, supported_b),
        "| 结论支持率 | %.4f | %.4f | %+.4f |"
        % (
            result["baseline"]["claim_support_rate"],
            result["verified"]["claim_support_rate"],
            result["delta"]["claim_support_rate"],
        ),
        "| 强结论输出数 | %d | %d | %+d |"
        % (
            result["baseline"]["strong_claims"],
            result["verified"]["strong_claims"],
            result["delta"]["strong_claims"],
        ),
        "| 拦截数（V1/V2，按当前规则） | — | %d | — |"
        % result["verified"]["blocked_by_current_rules"],
        "| 降级数（V3） | — | %d | — |" % result["verified"]["downgraded"],
        "| 整体判 unsupported | — | %d | — |"
        % result["verified"]["unsupported_runs"],
        "| 平均延迟(ms) | %d | %d | %+d |"
        % (
            result["baseline"]["latency_ms_mean"],
            result["verified"]["latency_ms_mean"],
            result["delta"]["latency_ms_mean"],
        ),
        "| 成本(元) | %.6f | %.6f | %+.6f |"
        % (
            result["baseline"]["cost_cny"],
            result["verified"]["cost_cny"],
            result["delta"]["cost_cny"],
        ),
        "",
        "说明：**无验证层那一列是用同一套机械规则离线复评出来的**，",
        "不是模型自评；误拦截率与漏放率需要独立判定者，按纪律不做 LLM judge，故未给出。",
        "",
    ]
    (out_dir / "compare.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


def summarize(rows):
    """汇总：完成率、追溯链完整率，以及 M1 验收项完成情况。"""
    total = len(rows)
    answered = [row for row in rows if row["status"] == "answered"]
    complete = [row for row in answered if row.get("trace_complete")]
    return {
        "tasks": total,
        "answered": len(answered),
        "asked_clarification": len(
            [row for row in rows if row.get("clarifications_asked")]
        ),
        "refused": len([row for row in rows if row["status"] == "refused"]),
        "errors": len([row for row in rows if row["status"] == "error"]),
        "answer_rate": round(len(answered) / total, 4) if total else 0.0,
        "trace_complete_rate": round(len(complete) / total, 4) if total else 0.0,
        "plan_present_rate": round(
            len([row for row in answered if row.get("plan_steps")]) / total, 4
        )
        if total
        else 0.0,
        "cost_cny_total": round(
            sum(row.get("cost_cny") or 0.0 for row in rows), 6
        ),
        "acceptance": _acceptance(rows),
        "verification": _verification_stats(rows),
        "todo_metrics": [
            "结论支持率",
            "误拦截率与漏放率（需独立判定者，按纪律不用 LLM judge）",
            "边界降级正确率（holdout）",
        ],
    }


def _verification_stats(rows):
    """验证层产出：支持率、拦截数、降级数、强结论数。"""
    answered = [row for row in rows if row.get("status") == "answered"]
    claims = sum(row.get("claims_total") or 0 for row in answered)
    supported = sum(row.get("claims_supported") or 0 for row in answered)
    return {
        "answered": len(answered),
        "claims_total": claims,
        "claims_supported": supported,
        "claim_support_rate": round(supported / claims, 4) if claims else 0.0,
        "blocked": sum(row.get("blocked") or 0 for row in answered),
        "downgraded": sum(row.get("downgraded") or 0 for row in answered),
        "strong_claims": len([row for row in answered if row.get("strong_claim")]),
        "unsupported_runs": len(
            [row for row in answered if row.get("verify_status") == "unsupported"]
        ),
    }


def _acceptance(rows):
    """M1 验收项：≥8 正常分析、≥4 澄清/拒答、结论含计划与证据链。"""
    normal = [row for row in rows if row.get("kind") == "normal"]
    boundary = [row for row in rows if row.get("kind") in ("clarify", "refuse")]
    normal_ok = [
        row
        for row in normal
        if row["status"] == "answered"
        and row.get("plan_steps")
        and row.get("trace_complete")
    ]
    clarify_ok = [
        row
        for row in boundary
        if row.get("status") == "refused" or row.get("clarifications_asked")
    ]
    return {
        "normal_total": len(normal),
        "normal_ok": len(normal_ok),
        "normal_pass": len(normal_ok) >= 8,
        "boundary_total": len(boundary),
        "boundary_ok": len(clarify_ok),
        "boundary_pass": len(clarify_ok) >= 4,
        "refused_expected": len(
            [row for row in rows if row.get("kind") == "refuse"]
        ),
        "refused_actual": len(
            [row for row in rows if row.get("kind") == "refuse"
             and row["status"] == "refused"]
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eval.runner")
    parser.add_argument("--tasks", default="")
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--verify",
        choices=("on", "off"),
        default="on",
        help="on=带验证层；off=M1 基线（用于对照）",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BASELINE_DIR", "VERIFIED_DIR"),
        help="离线复评基线结论并与带验证层的 run 对照",
    )
    args = parser.parse_args(argv)

    if args.compare:
        return compare(args.compare[0], args.compare[1])
    if not args.tasks:
        parser.error("跑批需要 --tasks；只做对照请用 --compare")
    payload = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    verify_layer = args.verify == "on"
    rows = [run_task(task, verify_layer) for task in payload["tasks"]]
    summary = summarize(rows)
    summary["verify_layer"] = verify_layer
    summary["run_id"] = uuid.uuid4().hex[:8]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
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
