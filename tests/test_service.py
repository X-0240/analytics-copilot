"""服务层测试：接口边界、会话隔离、错误码映射、超时映射。"""

import pytest
from fastapi.testclient import TestClient

from analytics_copilot import agent, db, service

client = TestClient(service.app)


def test_healthz_reports_whitelist():
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert set(body["allowed_views"]) == {"v_sales", "v_returns"}


def test_ask_returns_conclusion_and_evidence():
    body = client.post(
        "/ask", json={"question": "最近三个月销售额为什么下降？按国家看"}
    ).json()
    assert body["status"] == "answered"
    assert body["claim"] and body["evidence"]
    assert body["session_id"]


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
    def boom(question, session):
        raise db.SqlRejected("NOT_WHITELISTED", "对象不在白名单内")

    monkeypatch.setattr(agent, "analyze", boom)
    response = client.post("/ask", json={"question": "最近销售额怎么样"})
    assert response.status_code == 422
    assert response.json()["code"] == "NOT_WHITELISTED"


def test_timeout_maps_to_504(monkeypatch):
    def slow(question, session):
        raise service.asyncio.TimeoutError()

    monkeypatch.setattr(agent, "analyze", slow)
    response = client.post("/ask", json={"question": "最近销售额怎么样"})
    assert response.status_code == 504
    assert response.json()["code"] == "TIMEOUT"


def test_metrics_endpoint_counts_requests():
    before = client.get("/metrics").json()["requests"]
    client.get("/healthz")
    client.post("/ask", json={"question": "最近销售额为什么下降？按国家看"})
    after = client.get("/metrics").json()["requests"]
    assert after >= before + 1


@pytest.fixture(autouse=True)
def _clean_sessions():
    """每个用例前后清空会话表，避免相互污染。"""
    service.SESSIONS.clear()
    yield
    service.SESSIONS.clear()
