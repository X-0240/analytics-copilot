"""服务层测试：接口边界、会话隔离、错误码映射、超时映射 + M5 公网防护。"""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from analytics_copilot import agent, config, db, service

client = TestClient(service.app)


def test_healthz_reports_whitelist():
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert set(body["allowed_views"]) == {"v_sales", "v_returns"}
    assert body["failed"] == []


def test_index_serves_demo_page():
    """公网 Demo 的入口页必须能打开（不是只有一个 JSON 接口）。"""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Analytics Copilot" in response.text


def test_ask_returns_conclusion_and_evidence():
    body = client.post(
        "/ask", json={"question": "最近三个月销售额为什么下降？按国家看"}
    ).json()
    assert body["status"] == "answered"
    assert body["claim"] and body["evidence"]
    assert body["session_id"]
    assert body["request_id"]


def test_ask_asks_for_clarification_when_metric_missing():
    body = client.post(
        "/ask", json={"question": "最近情况怎么样", "session_id": "s-clarify"}
    ).json()
    assert body["status"] == "need_clarification"
    assert body["missing"]


def test_sessions_are_isolated():
    """不同 session 的澄清轮数互不影响。"""
    first = client.post(
        "/ask", json={"question": "最近情况", "session_id": "s-a"}
    ).json()
    second = client.post(
        "/ask", json={"question": "最近销售额", "session_id": "s-b"}
    ).json()
    assert first["session_id"] == "s-a" and second["session_id"] == "s-b"
    assert service.SESSIONS["s-a"]["clarify_rounds"] == 1


def test_sql_rejection_maps_to_422(monkeypatch):
    def boom(question, session, *args, **kwargs):
        raise db.SqlRejected("NOT_WHITELISTED", "对象不在白名单内")

    monkeypatch.setattr(agent, "analyze", boom)
    response = client.post("/ask", json={"question": "最近销售额怎么样"})
    assert response.status_code == 422
    assert response.json()["code"] == "NOT_WHITELISTED"


def test_timeout_maps_to_504(monkeypatch):
    def slow(question, session, *args, **kwargs):
        raise service.asyncio.TimeoutError()

    monkeypatch.setattr(agent, "analyze", slow)
    response = client.post("/ask", json={"question": "最近销售额怎么样"})
    assert response.status_code == 504
    assert response.json()["code"] == "TIMEOUT"


def test_metrics_endpoint_counts_requests():
    before = client.get("/metrics").json()["requests"]
    client.get("/healthz")
    client.post("/ask", json={"question": "最近销售额为什么下降？按国家看"})
    body = client.get("/metrics").json()
    assert body["requests"] >= before + 1
    assert "cost_cny_today" in body
    assert "latency_ms_p95" in body


def test_healthz_reports_degraded_when_db_unreadable(monkeypatch, tmp_path):
    """数据库读不到时必须 503，不能假健康。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "missing.duckdb")
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "db" in response.json()["failed"]


def test_body_over_limit_is_rejected_before_parsing(monkeypatch):
    """超大请求体在解析前就被挡掉，返回 413。"""
    monkeypatch.setattr(config, "MAX_BODY_BYTES", 512)
    response = client.post("/ask", json={"question": "销售额" * 500})
    assert response.status_code == 413
    assert response.json()["code"] == "BODY_TOO_LARGE"


def test_session_id_length_is_capped():
    response = client.post(
        "/ask", json={"question": "最近销售额怎么样", "session_id": "s" * 200}
    )
    assert response.status_code == 422


def test_rate_limit_blocks_burst(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 1)
    first = client.post("/ask", json={"question": "最近销售额怎么样"})
    second = client.post("/ask", json={"question": "最近销售额怎么样"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["code"] == "RATE_LIMITED"


def test_demo_token_required_when_configured(monkeypatch):
    """配了访问码就必须带对；不带或带错一律 401。"""
    monkeypatch.setattr(config, "DEMO_TOKEN", "demo-secret")
    body = {"question": "最近销售额怎么样"}
    assert client.post("/ask", json=body).status_code == 401
    assert client.post(
        "/ask", json=body, headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    ok = client.post(
        "/ask", json=body, headers={"Authorization": "Bearer demo-secret"}
    )
    assert ok.status_code == 200
    assert client.get("/healthz").json()["auth_required"] is True


def test_demo_token_absent_means_open(monkeypatch):
    """没配访问码时本地/评测路径不受影响。"""
    monkeypatch.setattr(config, "DEMO_TOKEN", "")
    assert client.post("/ask", json={"question": "最近销售额怎么样"}).status_code == 200
    assert client.get("/healthz").json()["auth_required"] is False


def test_budget_breaker_blocks_when_spent(monkeypatch):
    monkeypatch.setattr(config, "DAILY_COST_BUDGET_CNY", 1.0)
    service.RUNTIME["cost_today"] = 1.5
    response = client.post("/ask", json={"question": "最近销售额怎么样"})
    assert response.status_code == 503
    assert response.json()["code"] == "BUDGET_EXCEEDED"


def test_failed_attempts_are_still_charged(monkeypatch):
    """失败路径（做不到）也要记账，否则预算被低估。"""

    def fake(question, session, *args, **kwargs):
        meter = args[-1] if args else kwargs.get("meter")
        meter.add(0.25)
        meter.add(0.25)
        raise agent.UnableToComplete("两次都没成功", cost_cny=meter.total)

    monkeypatch.setattr(agent, "analyze", fake)
    body = client.post("/ask", json={"question": "最近销售额怎么样"}).json()
    assert body["status"] == "unable_to_complete"
    assert client.get("/metrics").json()["cost_cny_today"] == 0.5


def test_llm_failure_maps_to_502(monkeypatch):
    def boom(question, session, *args, **kwargs):
        raise service.llm.LLMError("HTTP_503")

    monkeypatch.setattr(agent, "analyze", boom)
    response = client.post("/ask", json={"question": "最近销售额怎么样"})
    assert response.status_code == 502
    assert response.json()["code"] == "LLM_UNAVAILABLE"


def test_log_keeps_hash_not_question_text(monkeypatch, tmp_path):
    """日志不留原文：只留 hash 与长度。"""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "USAGE_LOG", tmp_path / "usage.jsonl")
    question = "最近三个月销售额为什么下降？按国家看"
    client.post("/ask", json={"question": question})
    text = (tmp_path / "usage.jsonl").read_text(encoding="utf-8")
    assert question not in text
    record = json.loads(text.strip().splitlines()[-1])
    assert record["question_sha256"] == hashlib.sha256(
        question.encode("utf-8")
    ).hexdigest()[:16]
    assert record["question_len"] == len(question)
    assert record["request_id"]


def test_sessions_expire_by_ttl(monkeypatch):
    """空闲超时的会话被回收，内存不会无限涨。"""
    monkeypatch.setattr(config, "SESSION_TTL_SECONDS", -1.0)
    client.post("/ask", json={"question": "最近情况", "session_id": "s-ttl"})
    client.post("/ask", json={"question": "最近销售额", "session_id": "s-new"})
    assert "s-ttl" not in service.SESSIONS


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空进程内状态（会话、计数器、限流桶、成本），避免互相污染。"""
    service.reset_runtime_state()
    yield
    service.reset_runtime_state()
