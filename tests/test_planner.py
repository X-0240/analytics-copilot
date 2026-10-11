"""规划器护栏测试：结构、白名单、步数、引用，全部机械可判。"""

import json

import pytest

from analytics_copilot import db, planner


def _plan(**overrides):
    payload = {
        "scope": "ok",
        "assumptions": [],
        "steps": [
            {
                "name": "总量",
                "purpose": "看趋势",
                "sql": "SELECT COUNT(*) AS n FROM v_sales",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_valid_plan_passes_and_gets_indexes():
    got = planner._validate_plan(_plan())
    assert got["scope"] == "ok"
    assert got["steps"][0]["index"] == 1


def test_plan_referencing_non_whitelisted_table_is_rejected():
    bad = _plan(
        steps=[
            {
                "name": "越界",
                "purpose": "x",
                "sql": "SELECT * FROM raw_online_retail",
            }
        ]
    )
    with pytest.raises(db.SqlRejected) as exc:
        planner._validate_plan(bad)
    assert exc.value.code == "NOT_WHITELISTED"


def test_plan_with_too_many_steps_is_rejected():
    step = {
        "name": "s",
        "purpose": "p",
        "sql": "SELECT COUNT(*) AS n FROM v_sales",
    }
    with pytest.raises(planner.PlanInvalid) as exc:
        planner._validate_plan(_plan(steps=[step] * (planner.MAX_STEPS + 1)))
    assert exc.value.code == "PLAN_TOO_MANY_STEPS"


def test_refuse_requires_reason():
    with pytest.raises(planner.PlanInvalid) as exc:
        planner._validate_plan({"scope": "refuse", "refuse_reason": "  "})
    assert exc.value.code == "PLAN_REFUSE_NO_REASON"


def test_scope_hint_detects_out_of_scope_words():
    assert planner.scope_hint("预测下个月的销售额")
    assert planner.scope_hint("哪个月利润最高")
    assert not planner.scope_hint("各国销售额占比")


def test_extract_json_tolerates_code_fence():
    raw = "```json\n" + json.dumps(_plan(), ensure_ascii=False) + "\n```"
    assert planner._extract_json(raw)["scope"] == "ok"
