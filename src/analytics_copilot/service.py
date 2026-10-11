"""HTTP 服务层：接口边界、会话隔离、防护、日志、成本、超时、错误处理。

M5 公网部署在 M0 骨架上补了四类防护（方案见 `docs/M5_部署方案.md`）：

1. **输入**：请求体按字节限流（ASGI 层，分块请求也拦得住）、`session_id` 长度上限；
2. **滥用**：单 IP 限流 + 在飞请求上限 + 会话数上限与空闲回收；
3. **钱**：每次请求一个 `llm.CostMeter`，把计划/重试/结论的调用全记上，
   对照当日预算熔断（预算消耗从 `usage.jsonl` 回填，重启不丢）；
4. **隐私**：日志不留问题原文，只留 `question_sha256` 与长度。

其他既有语义保持不变：`/ask` 返回结构化结论 + 证据链；澄清 200、拒答 200、
做不到 200（带 code）、SQL 被拒 422、超时 504、内部错误 500。
"""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import date, datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from analytics_copilot import (
    agent,
    capabilities,
    config,
    db,
    llm,
    tools,
    versions,
)

app = FastAPI(title="Analytics Copilot", version=versions.APP_VERSION)

SESSIONS: dict[str, dict] = {}
COUNTERS = {
    "requests": 0,
    "clarify": 0,
    "refused": 0,
    "unable": 0,
    "errors": 0,
    "timeouts": 0,
    "rate_limited": 0,
    "inflight_rejected": 0,
    "budget_blocked": 0,
    "body_too_large": 0,
    "unauthorized": 0,
}

RUNTIME = {
    "cost_today": 0.0,
    "cost_total": 0.0,
    "latencies": deque(maxlen=500),
    "rate": defaultdict(deque),
    "inflight": 0,
    "day": date.today().isoformat(),
}


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    session_id: str | None = Field(default=None, max_length=64)


class BodyLimitMiddleware:
    """按**实际收到的字节数**限流请求体。

    不依赖 `Content-Length`：分块（chunked）请求没有该头，只看头会放过大包。
    这里**先把请求体读进来**（上限之内），超限直接回 413 且不调用下游；
    没超限就把已读内容原样交回下游（Starlette 的 receive 是一次性的，
    不重放的话 FastAPI 会拿到空 body）。

    为什么不用"抛异常让下层冒出来"：FastAPI 解析 body 时会把任何异常包成
    HTTP 400，冒不到本层，所以必须在进入下游之前就判断。
    """

    def __init__(self, app, max_bytes=None):
        self.app = app
        # 留 None 表示"每次请求读配置"，这样调参不需要重建 app（测试也要靠它）
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        limit = self.max_bytes or config.MAX_BODY_BYTES
        chunks, received, more_body = [], 0, True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            chunk = message.get("body") or b""
            received += len(chunk)
            if received > limit:
                COUNTERS["body_too_large"] += 1
                response = JSONResponse(
                    status_code=413,
                    content={
                        "status": "error",
                        "code": "BODY_TOO_LARGE",
                        "message": "请求体超过 %d 字节上限" % limit,
                        "http_status": 413,
                    },
                )
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            more_body = bool(message.get("more_body"))

        body = b"".join(chunks)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


app.add_middleware(BodyLimitMiddleware)


def reset_runtime_state():
    """把进程内状态清回初始（测试与控制台排查用）。"""
    SESSIONS.clear()
    for key in COUNTERS:
        COUNTERS[key] = 0
    RUNTIME["cost_today"] = 0.0
    RUNTIME["cost_total"] = 0.0
    RUNTIME["latencies"].clear()
    RUNTIME["rate"].clear()
    RUNTIME["inflight"] = 0
    RUNTIME["day"] = date.today().isoformat()


def _client_ip(request):
    # 部署在隧道/反向代理后面时取 X-Forwarded-For 的第一段，否则用直连地址
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip):
    """滑动窗口限流：默认每 IP 每分钟 N 次。"""
    limit = config.RATE_LIMIT_PER_MIN
    if limit <= 0:
        return False
    now = time.monotonic()
    bucket = RUNTIME["rate"][ip]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


def _authorized(request):
    """Demo 访问码校验。

    未配置 `AC_DEMO_TOKEN` 时一律放行（本地直跑与评测默认如此）；
    配置了就必须带 `Authorization: Bearer <token>`（也接受 `X-Demo-Token`）。
    用 `compare_digest` 做定时比较，避免按字符比较泄漏长度与内容。
    """
    token = config.DEMO_TOKEN
    if not token:
        return True
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return hmac.compare_digest(header[7:].strip(), token)
    return hmac.compare_digest(request.headers.get("x-demo-token", ""), token)


def _prune_sessions():
    """清掉空闲超时的会话；超上限时按最久未访问淘汰。"""
    now = time.monotonic()
    ttl = config.SESSION_TTL_SECONDS
    for key in [k for k, v in SESSIONS.items() if now - v["last_seen"] > ttl]:
        SESSIONS.pop(key, None)
    while len(SESSIONS) >= config.SESSION_MAX:
        oldest = min(SESSIONS.items(), key=lambda item: item[1]["last_seen"])[0]
        SESSIONS.pop(oldest, None)


def _session(session_id):
    """取或建会话状态；默认每一轮澄清都算在同一会话里。"""
    _prune_sessions()
    now = time.monotonic()
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "clarify_rounds": 0,
            "history": [],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "last_seen": now,
        }
    SESSIONS[session_id]["last_seen"] = now
    return SESSIONS[session_id]


def _roll_log_if_needed(path):
    """按大小轮转：usage.jsonl → .1 → .2 …，超过备份数就丢弃最旧的。"""
    try:
        if not path.exists() or path.stat().st_size < config.LOG_MAX_BYTES:
            return
        for index in range(config.LOG_BACKUPS, 1, -1):
            older = path.with_name(path.name + ".%d" % (index - 1))
            newer = path.with_name(path.name + ".%d" % index)
            if older.exists():
                older.replace(newer)
        path.replace(path.with_name(path.name + ".1"))
    except OSError as exc:  # 轮转失败不应让请求失败
        print("[warn] 日志轮转失败：%s" % exc)


def _log(record):
    """把一次请求追加到使用日志（失败不影响主流程）。"""
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        _roll_log_if_needed(config.USAGE_LOG)
        with config.USAGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # 日志失败不应让请求失败
        print("[warn] 写日志失败：%s" % exc)


def _load_cost_today():
    """启动时从日志回填当日成本，这样重启不会把预算清零。"""
    today = date.today().isoformat()
    total = 0.0
    try:
        if not config.USAGE_LOG.exists():
            return 0.0
        with config.USAGE_LOG.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("at", "")).startswith(today):
                    total += float(row.get("cost_cny") or 0.0)
    except OSError:
        return 0.0
    return total


def _budget_exceeded():
    """当日成本是否已到预算上限（0 或负值表示不限制）。"""
    budget = config.DAILY_COST_BUDGET_CNY
    if budget <= 0:
        return False
    return RUNTIME["cost_today"] >= budget


def _record_cost(cost):
    """累计成本；跨天时把"今日"清零。"""
    today = date.today().isoformat()
    if RUNTIME["day"] != today:
        RUNTIME["day"] = today
        RUNTIME["cost_today"] = 0.0
    cost = float(cost or 0.0)
    RUNTIME["cost_today"] += cost
    RUNTIME["cost_total"] += cost


def _percentile(values, ratio):
    if not values:
        return 0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * ratio))
    return int(ordered[index])


@app.get("/")
def index():
    """最小演示页面：给非技术访问者一个能直接提问的入口（不是前端框架工程）。"""
    page = config.ROOT / "web" / "index.html"
    if not page.exists():
        return JSONResponse(
            status_code=404,
            content={"status": "error", "code": "NO_PAGE", "message": "页面文件缺失"},
        )
    return FileResponse(page)


@app.get("/healthz")
def healthz():
    """存活 + 就绪：API、DuckDB 可读、元数据可加载，三者缺一即 503。

    之前只读元数据，数据库坏了也会返回 200（假健康），现在必须真查一次库。
    """
    failed = []
    meta = {}
    try:
        meta = db.load_metadata()
    except Exception:  # noqa: BLE001 - 健康检查要吞掉细节，只报哪一项坏了
        failed.append("metadata")
    try:
        db.run_sql("SELECT 1 AS ok FROM v_sales LIMIT 1", max_rows=1)
    except Exception:  # noqa: BLE001
        failed.append("db")
    body = {
        "status": "ok" if not failed else "degraded",
        "failed": failed,
        "db_rows": (meta.get("profile") or {}).get("rows"),
        "allowed_views": meta.get("allowed_views", []),
        "stub": config.FORCE_STUB,
        # 只回"要不要访问码"，不回访问码本身
        "auth_required": bool(config.DEMO_TOKEN),
        # 版本标识：报告里引用哪个数字，就能对上哪一版
        "versions": versions.stamp(),
    }
    if failed:
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/capabilities")
def capability_card():
    """数据能力卡：用户先知道能问什么，拒答时也引用这份内容。"""
    return capabilities.card()


@app.get("/metrics")
def metrics():
    """极简指标：够用即可，不引监控栈（避免为展示复杂架构加组件）。"""
    latencies = RUNTIME["latencies"]
    budget = config.DAILY_COST_BUDGET_CNY
    return {
        **COUNTERS,
        "cost_cny_total": round(RUNTIME["cost_total"], 6),
        "cost_cny_today": round(RUNTIME["cost_today"], 6),
        "budget_remaining_cny": (
            None if budget <= 0 else round(max(budget - RUNTIME["cost_today"], 0), 6)
        ),
        "latency_ms_p50": _percentile(latencies, 0.5),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "inflight": RUNTIME["inflight"],
        "sessions": len(SESSIONS),
    }


@app.post("/ask")
async def ask(payload: AskRequest, request: Request):
    """唯一业务接口：业务问题 → 结论 + 证据链（或澄清请求）。"""
    ip = _client_ip(request)
    request_id = uuid.uuid4().hex[:8]
    session_id = payload.session_id or uuid.uuid4().hex[:12]
    record = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "request_id": request_id,
        "session_id": session_id,
        "client": ip,
        "question_sha256": hashlib.sha256(
            payload.question.encode("utf-8")
        ).hexdigest()[:16],
        "question_len": len(payload.question),
    }
    if _rate_limited(ip):
        COUNTERS["rate_limited"] += 1
        record.update({"status": "rate_limited"})
        _log(record)
        return _error(
            429, session_id, "RATE_LIMITED", "请求过于频繁，请稍后再试", request_id
        )
    if not _authorized(request):
        COUNTERS["unauthorized"] += 1
        record.update({"status": "unauthorized"})
        _log(record)
        return _error(
            401,
            session_id,
            "UNAUTHORIZED",
            "需要访问码：请在页面填写，或用 Authorization: Bearer <token> 调用",
            request_id,
        )
    if RUNTIME["inflight"] >= config.MAX_INFLIGHT:
        COUNTERS["inflight_rejected"] += 1
        record.update({"status": "busy"})
        _log(record)
        return _error(503, session_id, "BUSY", "服务繁忙，请稍后再试", request_id)
    if _budget_exceeded():
        COUNTERS["budget_blocked"] += 1
        record.update({"status": "budget_blocked"})
        _log(record)
        return _error(
            503,
            session_id,
            "BUDGET_EXCEEDED",
            "当日演示预算已用完，请明天再试（可调整 AC_DAILY_COST_BUDGET_CNY）",
            request_id,
        )

    session = _session(session_id)
    meter = llm.CostMeter()
    COUNTERS["requests"] += 1
    RUNTIME["inflight"] += 1
    started = time.perf_counter()
    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(
                agent.analyze, payload.question, session, None, True, meter
            ),
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
    except agent.ClarifyNeeded as exc:
        COUNTERS["clarify"] += 1
        record.update({"status": "need_clarification", "cost_cny": 0.0})
        _log(record)
        return {
            "session_id": session_id,
            "request_id": request_id,
            "status": "need_clarification",
            "missing": exc.missing,
            "ask": exc.question,
        }
    except agent.Refused as exc:
        # 拒答是正常业务结果（超出能力边界），不是错误：HTTP 200 + 明确原因
        COUNTERS["refused"] += 1
        _record_cost(getattr(exc, "cost_cny", 0.0))
        record.update(
            {"status": "refused", "cost_cny": round(getattr(exc, "cost_cny", 0.0), 6)}
        )
        _log(record)
        return {
            "session_id": session_id,
            "request_id": request_id,
            "status": "refused",
            "reason": exc.reason,
        }
    except agent.UnableToComplete as exc:
        # 降级：分析做不出来时给结构化说明，而不是 500
        COUNTERS["unable"] += 1
        _record_cost(getattr(exc, "cost_cny", 0.0))
        record.update(
            {
                "status": "unable_to_complete",
                "code": exc.code,
                "cost_cny": round(getattr(exc, "cost_cny", 0.0), 6),
            }
        )
        _log(record)
        return {
            "session_id": session_id,
            "request_id": request_id,
            "status": "unable_to_complete",
            "code": exc.code,
            "reason": exc.reason,
        }
    except db.SqlRejected as exc:
        COUNTERS["errors"] += 1
        _record_cost(meter.total)
        record.update({"status": "rejected", "code": exc.code})
        _log(record)
        return _error(422, session_id, exc.code, str(exc), request_id)
    except llm.LLMError as exc:
        # 上游模型不可用属环境故障：单列一类，便于从指标上区分"模型挂了"
        COUNTERS["errors"] += 1
        _record_cost(meter.total)
        record.update({"status": "llm_error", "code": "LLM_UNAVAILABLE"})
        _log(record)
        return _error(
            502, session_id, "LLM_UNAVAILABLE", "模型服务不可用：%s" % exc, request_id
        )
    except asyncio.TimeoutError:
        COUNTERS["timeouts"] += 1
        _record_cost(meter.total)
        record.update({"status": "timeout"})
        _log(record)
        return _error(504, session_id, "TIMEOUT", "分析超时，请缩小问题范围", request_id)
    except Exception as exc:
        COUNTERS["errors"] += 1
        _record_cost(meter.total)
        record.update({"status": "internal_error", "error": type(exc).__name__})
        _log(record)
        return _error(
            500, session_id, "INTERNAL", "内部错误：%s" % type(exc).__name__, request_id
        )
    finally:
        RUNTIME["inflight"] -= 1

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    cost = round(meter.total if meter.calls else outcome.get("cost_cny", 0.0), 6)
    _record_cost(cost)
    RUNTIME["latencies"].append(elapsed_ms)
    conclusion = outcome["conclusion"]
    queries = [call.input.get("sql", "") for call in outcome["tool_calls"]]
    session["history"].append(payload.question)
    record.update(
        {
            "status": "answered",
            "elapsed_ms": elapsed_ms,
            "queries": len(queries),
            "mode": outcome.get("mode", "rule"),
            "cost_cny": cost,
            "confidence": conclusion.confidence,
            "caveats": len(conclusion.caveats),
            "explanation_level": outcome.get("explanation_level", 1),
        }
    )
    _log(record)
    return {
        "session_id": session_id,
        "request_id": request_id,
        "status": "answered",
        "mode": outcome.get("mode", "rule"),
        "claim": conclusion.claim,
        "confidence": conclusion.confidence,
        "caveats": conclusion.caveats,
        "evidence": conclusion.evidence,
        "plan": outcome.get("plan", {}),
        "claims": outcome.get("claims", []),
        "verification": outcome.get("verification", {"status": "not_run"}),
        "explanation_level": outcome.get("explanation_level", 1),
        "level_violations": outcome.get("level_violations", []),
        # 口径（含本次时间窗解析结果）：SQL 与答案同源，供前端展示与复核
        "time_window": outcome.get("time_window", {}),
        "analysis_context": outcome.get("analysis_context", {}),
        "next_questions": outcome.get("next_questions", []),
        "cost_cny": cost,
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


def _error(status, session_id, code, message, request_id=None):
    """统一的错误返回体：HTTP 状态与 code 都要正确，前端与评测都能直接读。"""
    return JSONResponse(
        status_code=status,
        content={
            "session_id": session_id,
            "request_id": request_id,
            "status": "error",
            "code": code,
            "message": message,
            "http_status": status,
        },
    )


def meta_snapshot():
    """给评测脚本用的元数据快照（避免评测直接依赖内部结构）。"""
    return asdict(tools.metadata_tool())


RUNTIME["cost_today"] = _load_cost_today()
