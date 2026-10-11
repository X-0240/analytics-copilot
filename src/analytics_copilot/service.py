"""HTTP 服务层（M0 骨架）：接口边界、会话隔离、日志、成本、超时、错误处理。

设计边界（为 M5 的公网单实例部署留好接口，但 M0 不部署）：

- **接口边界**：只有一个业务接口 `POST /ask`，返回结构化结论 + 证据链；
- **会话隔离**：按 `session_id` 分开保存澄清轮数与假设；M0 是单进程内存态，
  多实例部署会失效——这一点写在这里，不假装已经有了分布式会话；
- **日志**：每次请求追加一行 JSON 到 `logs/usage.jsonl`（含状态、查询数、耗时、成本）；
- **成本**：M0 走桩恒为 0，但记账点已经就位（换真实模型时只改 `_cost_of`）；
- **超时**：请求级 `asyncio.wait_for`，超时返回 504 并记日志；
- **错误**：SQL 护栏拒绝 → 422 带 code；未预期异常 → 500，同样落日志。
"""

import asyncio
import json
import time
import uuid
from dataclasses import asdict
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from analytics_copilot import agent, config, db, tools

app = FastAPI(title="Analytics Copilot (M0)", version="0.1.0")

SESSIONS: dict[str, dict] = {}
COUNTERS = {
    "requests": 0,
    "clarify": 0,
    "refused": 0,
    "errors": 0,
    "timeouts": 0,
}


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    session_id: str | None = None


def _session(session_id):
    """取或建会话状态；默认每一轮澄清都算在同一会话里。"""
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "clarify_rounds": 0,
            "history": [],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    return SESSIONS[session_id]


def _log(record):
    """把一次请求追加到使用日志（失败不影响主流程）。"""
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with config.USAGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # 日志失败不应让请求失败
        print("[warn] 写日志失败：%s" % exc)


@app.get("/healthz")
def healthz():
    """存活与数据可达性；同时回显白名单，便于部署后核对。"""
    meta = db.load_metadata()
    return {
        "status": "ok",
        "db_rows": meta["profile"]["rows"],
        "allowed_views": meta["allowed_views"],
        "stub": config.FORCE_STUB,
    }


@app.get("/metrics")
def metrics():
    """极简指标：够用即可，不引监控栈（避免为展示复杂架构加组件）。"""
    return dict(COUNTERS)


@app.post("/ask")
async def ask(payload: AskRequest):
    """唯一业务接口：业务问题 → 结论 + 证据链（或澄清请求）。"""
    session_id = payload.session_id or uuid.uuid4().hex[:12]
    session = _session(session_id)
    COUNTERS["requests"] += 1
    started = time.perf_counter()
    record = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "session_id": session_id,
        "question": payload.question,
    }
    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(agent.analyze, payload.question, session),
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
    except agent.ClarifyNeeded as exc:
        COUNTERS["clarify"] += 1
        record.update({"status": "need_clarification", "missing": exc.missing})
        _log(record)
        return {
            "session_id": session_id,
            "status": "need_clarification",
            "missing": exc.missing,
            "ask": exc.question,
        }
    except agent.Refused as exc:
        # 拒答是正常业务结果（超出能力边界），不是错误：HTTP 200 + 明确原因
        COUNTERS["refused"] = COUNTERS.get("refused", 0) + 1
        record.update({"status": "refused", "reason": exc.reason})
        _log(record)
        return {
            "session_id": session_id,
            "status": "refused",
            "reason": exc.reason,
        }
    except db.SqlRejected as exc:
        COUNTERS["errors"] += 1
        record.update({"status": "rejected", "code": exc.code})
        _log(record)
        return _error(422, session_id, exc.code, str(exc))
    except asyncio.TimeoutError:
        COUNTERS["timeouts"] += 1
        record.update({"status": "timeout"})
        _log(record)
        return _error(504, session_id, "TIMEOUT", "分析超时，请缩小问题范围")
    except Exception as exc:
        COUNTERS["errors"] += 1
        record.update({"status": "internal_error", "error": type(exc).__name__})
        _log(record)
        return _error(500, session_id, "INTERNAL", "内部错误：%s" % type(exc).__name__)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    conclusion = outcome["conclusion"]
    queries = [call.input.get("sql", "") for call in outcome["tool_calls"]]
    session["history"].append(payload.question)
    record.update(
        {
            "status": "answered",
            "elapsed_ms": elapsed_ms,
            "queries": len(queries),
            "mode": outcome.get("mode", "rule"),
            "cost_cny": outcome.get("cost_cny", 0.0),
            "confidence": conclusion.confidence,
            "caveats": len(conclusion.caveats),
        }
    )
    _log(record)
    return {
        "session_id": session_id,
        "status": "answered",
        "mode": outcome.get("mode", "rule"),
        "claim": conclusion.claim,
        "confidence": conclusion.confidence,
        "caveats": conclusion.caveats,
        "evidence": conclusion.evidence,
        "plan": outcome.get("plan", {}),
        "cited_steps": outcome.get("cited_steps", []),
        "cost_cny": outcome.get("cost_cny", 0.0),
        "checks": [
            {"name": item.name, "ok": item.ok, "detail": item.detail}
            for item in outcome["checks"]
        ],
        "assumptions": getattr(outcome["understanding"], "assumptions", []),
        "tool_calls": [
            {"tool": call.tool, "rows": call.rows, "detail": call.detail}
            for call in outcome["tool_calls"]
        ],
        "elapsed_ms": elapsed_ms,
    }


def _error(status, session_id, code, message):
    """统一的错误返回体：HTTP 状态与 code 都要正确，前端与评测都能直接读。"""
    return JSONResponse(
        status_code=status,
        content={
            "session_id": session_id,
            "status": "error",
            "code": code,
            "message": message,
            "http_status": status,
        },
    )


def meta_snapshot():
    """给评测脚本用的元数据快照（避免评测直接依赖内部结构）。"""
    return asdict(tools.metadata_tool())
