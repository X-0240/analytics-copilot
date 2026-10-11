"""LLM 客户端：桩 + OpenAI 兼容实现（M1 用来生成分析计划与结论）。

纪律：
- 只读环境变量里的 key（`AC_LLM_API_KEY`），**不打印、不写入任何文件**；
- 桩是默认路径：没有 key 时链路照样可测、可复现，不因为缺 key 而失败；
- 温度 0 + JSON 输出；失败只记类型与状态码，不把请求体写进日志。
"""

import json
import urllib.error
import urllib.request

from analytics_copilot import config


class LLMError(RuntimeError):
    """LLM 层错误：网络、限流、返回体不合法都归到这里。"""


class CostMeter:
    """按请求累计模型花费（元）。

    为什么要单独一个计量器：一次分析可能调模型多次（计划 + 修复重试 + 结论），
    失败路径也会花钱。只在成功返回体里取 `cost_cny` 会**低估**预算消耗，
    所以把计量挂在客户端上，由调用方在每个请求开始时新建一个。
    """

    def __init__(self):
        self.total = 0.0
        self.calls = 0

    def add(self, cost):
        self.total += float(cost or 0.0)
        self.calls += 1


class LLMClient:
    """接口：只暴露一个方法，参数与返回都是纯 JSON 字符串。"""

    name = "base"
    meter: "CostMeter | None" = None

    def complete_json(self, system, user, max_tokens=900):
        raise NotImplementedError


class StubLLM(LLMClient):
    """确定性桩：M1 由 planner 提供计划，桩只处理"没有 key"的退化路径。"""

    name = "stub"

    def complete_json(self, system, user, max_tokens=900):
        raise LLMError("STUB_NO_PLAN: 桩不生成计划，请走规则路径")


class OpenAICompatLLM(LLMClient):
    """OpenAI 兼容端点（本项目按 DeepSeek 官方 API 使用）。"""

    name = "openai_compat"

    def __init__(self, base_url, api_key, model, timeout=60, meter=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.meter = meter

    def complete_json(self, system, user, max_tokens=900):
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError("HTTP_%d" % exc.code) from exc
        except Exception as exc:
            raise LLMError(type(exc).__name__) from exc
        usage = payload.get("usage") or {}
        cost = (
            usage.get("prompt_tokens", 0) / 1_000_000 * config.PRICE_IN_CNY_PER_1M
            + usage.get("completion_tokens", 0)
            / 1_000_000
            * config.PRICE_OUT_CNY_PER_1M
        )
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if not content.strip():
            # 空 output 在项目一踩过：不能当作成功，要显式报错
            raise LLMError(
                "EMPTY_CONTENT(finish_reason=%s)" % choice.get("finish_reason")
            )
        if self.meter is not None:
            # 记账点只有一个：拿到 usage 就记账（含最终失败的那次调用）
            self.meter.add(cost)
        return json.dumps({"content": content, "cost_cny": round(cost, 6)})


def get_client(meter=None):
    """有 key 就返回真实客户端，否则返回桩（默认离线可跑）。"""
    if config.FORCE_STUB or not config.LLM_API_KEY:
        return StubLLM()
    return OpenAICompatLLM(
        base_url=config.LLM_BASE_URL or "https://api.deepseek.com",
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL or "deepseek-flash",
        meter=meter,
    )
