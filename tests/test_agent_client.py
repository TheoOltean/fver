"""Request-shape tests against a fake anthropic client object. No network."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import anthropic
import pytest

from fver.prove.client import (
    FALLBACK_BETA,
    FatalAgentError,
    LLMClient,
    LLMSettings,
    RetryableAgentError,
)


@dataclass
class _Usage:
    input_tokens: int = 1000
    output_tokens: int = 100
    cache_read_input_tokens: int = 400
    cache_creation_input_tokens: int = 50


@dataclass
class _Block:
    type: str
    text: str = ""


@dataclass
class _Response:
    content: list = field(default_factory=lambda: [_Block("thinking"), _Block("text", "hello")])
    usage: _Usage = field(default_factory=_Usage)
    stop_reason: str = "end_turn"
    model: str = "claude-fable-5-1"
    stop_details: object = None
    _request_id: str = "req_1"


class _Stream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _FakeAnthropic:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []
        self.timeouts: list[float] = []
        self.messages = SimpleNamespace(stream=lambda **kw: self._stream("messages", kw))
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **kw: self._stream("beta", kw))
        )

    def with_options(self, timeout=None, **kw):
        self.timeouts.append(timeout)
        return self

    def _stream(self, api, kw):
        self.calls.append((api, kw))
        return _Stream(self.response)


def _client(settings, response=None, **kw):
    fake = _FakeAnthropic(response or _Response())
    return LLMClient(settings, client=fake, **kw), fake


def test_request_shape_with_fallbacks_and_caching(tmp_path):
    settings = LLMSettings(
        model="claude-fable-5-1", effort="xhigh", max_tokens=1234, timeout_seconds=42.0
    )
    c, fake = _client(settings, log_dir=tmp_path, run_id="run1")
    out = c.complete(["REF", "EX", "INSTR"], [{"role": "user", "content": "task"}], log_name="f-1")
    api, kw = fake.calls[0]
    assert api == "beta"
    assert "thinking" not in kw
    assert kw["model"] == "claude-fable-5-1" and kw["max_tokens"] == 1234
    assert kw["output_config"] == {"effort": "xhigh"}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in kw["system"])
    assert [b["text"] for b in kw["system"]] == ["REF", "EX", "INSTR"]
    assert kw["messages"] == [{"role": "user", "content": "task"}]
    assert fake.timeouts == [42.0]
    assert out.text == "hello" and out.request_id == "req_1" and not out.refused
    assert len(out.content_blocks) == 2  # thinking block preserved for replay
    assert out.cost.input_tokens == 1000 and out.cost.cache_read_tokens == 400
    assert out.cost.usd == pytest.approx(
        1000 * 10 / 1e6 + 100 * 50 / 1e6 + 400 * 0.25 / 1e6 + 50 * 12.5 / 1e6
    )
    assert (tmp_path / "llm" / "run1" / "f-1.json").exists()


def test_request_shape_without_fallbacks_or_caching():
    settings = LLMSettings(fallbacks=False, prompt_caching=False, effort="low")
    c, fake = _client(settings)
    c.complete(["REF"], [{"role": "user", "content": "t"}])
    api, kw = fake.calls[0]
    assert api == "messages"
    assert "betas" not in kw and "fallbacks" not in kw and "thinking" not in kw
    assert "cache_control" not in kw["system"][0]
    assert kw["output_config"]["effort"] == "low"


def test_effort_override():
    c, fake = _client(LLMSettings(effort="high"))
    c.complete(["R"], [{"role": "user", "content": "t"}], effort="max")
    assert fake.calls[0][1]["output_config"]["effort"] == "max"


def test_cache_breakpoints_capped_at_four():
    c, _ = _client(LLMSettings())
    req = c.build_request([f"b{i}" for i in range(6)], [{"role": "user", "content": "t"}])
    marked = [i for i, b in enumerate(req["system"]) if "cache_control" in b]
    assert marked == [2, 3, 4, 5]


def test_refusal_handled():
    resp = _Response(
        content=[],
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="cyber", explanation="nope"),
    )
    c, _ = _client(LLMSettings(), response=resp)
    out = c.complete(["R"], [{"role": "user", "content": "t"}])
    assert out.refused and out.text == "" and "cyber" in out.refusal_reason


def _http_error(cls, status):
    import httpx2 as httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, json={"error": {"message": "boom"}})
    return cls("boom", response=resp, body=None)


def test_auth_error_is_fatal():
    c, _ = _client(LLMSettings(), response=_http_error(anthropic.AuthenticationError, 401))
    with pytest.raises(FatalAgentError, match="ant auth login"):
        c.complete(["R"], [{"role": "user", "content": "t"}])


def test_bad_request_is_fatal():
    c, _ = _client(LLMSettings(), response=_http_error(anthropic.BadRequestError, 400))
    with pytest.raises(FatalAgentError, match="rejected"):
        c.complete(["R"], [{"role": "user", "content": "t"}])


def test_server_error_retried_then_raised(monkeypatch):
    monkeypatch.setattr("fver.prove.client.time.sleep", lambda s: None)
    c, fake = _client(
        LLMSettings(client_retries=2), response=_http_error(anthropic.InternalServerError, 500)
    )
    with pytest.raises(RetryableAgentError):
        c.complete(["R"], [{"role": "user", "content": "t"}])
    assert len(fake.calls) == 3


def test_rate_limit_is_retryable(monkeypatch):
    monkeypatch.setattr("fver.prove.client.time.sleep", lambda s: None)
    c, _fake = _client(
        LLMSettings(client_retries=0), response=_http_error(anthropic.RateLimitError, 429)
    )
    with pytest.raises(RetryableAgentError, match="rate limited"):
        c.complete(["R"], [{"role": "user", "content": "t"}])
