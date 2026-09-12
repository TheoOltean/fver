"""The one place fver talks to the Anthropic API.

Responsibilities: request shaping for the configured model (Fable 5.1 by
default), refusal handling with server-side fallbacks, cost accounting,
transcript logging, and turning SDK exceptions into a small error
hierarchy the loop can act on.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fver.core.models import Cost, now_iso
from fver.prove.pricing import cost_from_usage, price_for

log = logging.getLogger("fver.prove.client")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CACHE_BREAKPOINTS = 4


class AgentError(RuntimeError):
    """Base class for errors raised by the LLM client."""


class RetryableAgentError(AgentError):
    """Transient: rate limits, server errors, network. Safe to retry later."""


class FatalAgentError(AgentError):
    """Auth failures, bad requests: retrying will not help."""


@dataclass
class Completion:
    text: str
    content_blocks: list[Any]  # response.content, passed back unchanged on repair turns
    cost: Cost
    request_id: str | None = None
    stop_reason: str | None = None
    refused: bool = False
    refusal_reason: str = ""
    model_served: str = ""


@dataclass
class LLMSettings:
    api_key: str | None = None
    base_url: str | None = None
    model: str = "claude-fable-5-1"
    effort: str = "high"
    max_tokens: int = 32000
    fallbacks: bool = True
    prompt_caching: bool = True
    timeout_seconds: float = 1800.0
    max_retries: int = 4  # SDK-level retries for 429/5xx
    client_retries: int = 2  # our own retries on top, for RetryableAgentError


def _text_of(blocks: list[Any]) -> str:
    parts = []
    for b in blocks:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
    return "".join(parts)


class LLMClient:
    """Thin, synchronous wrapper around anthropic.Anthropic().

    `client` may be injected (tests pass a fake exposing `.with_options`,
    `.messages.stream` and `.beta.messages.stream`).
    """

    def __init__(
        self,
        settings: LLMSettings | None = None,
        log_dir: Path | None = None,
        run_id: str = "",
        client: Any | None = None,
    ):
        self.settings = settings or LLMSettings()
        self.log_dir = log_dir
        self.run_id = run_id
        self._client = client
        _, known = price_for(self.settings.model)
        if not known:
            log.warning(
                "Unknown model %s: costs will be estimated at Opus rates", self.settings.model
            )

    # -- construction -------------------------------------------------------

    def _anthropic(self) -> Any:
        if self._client is None:
            import anthropic

            kwargs: dict[str, Any] = {"max_retries": self.settings.max_retries}
            if self.settings.api_key:
                kwargs["api_key"] = self.settings.api_key
            if self.settings.base_url:
                kwargs["base_url"] = self.settings.base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    # -- request shaping ------------------------------------------------------

    def build_request(
        self, system_blocks: list[str], messages: list[dict], effort: str | None = None
    ) -> dict[str, Any]:
        """Return the kwargs for messages.stream(). Pure; used by tests."""
        s = self.settings
        system: list[dict[str, Any]] = []
        n = len(system_blocks)
        for i, text in enumerate(system_blocks):
            block: dict[str, Any] = {"type": "text", "text": text}
            # Breakpoints on the last <=4 blocks: caching is prefix-based, so a
            # breakpoint on block i caches everything up to and including it.
            if s.prompt_caching and i >= n - MAX_CACHE_BREAKPOINTS:
                block["cache_control"] = {"type": "ephemeral"}
            system.append(block)
        req: dict[str, Any] = {
            "model": s.model,
            "max_tokens": s.max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": effort or s.effort},
        }
        if s.fallbacks:
            req["betas"] = [FALLBACK_BETA]
            req["fallbacks"] = "default"
        return req

    # -- the call -----------------------------------------------------------------

    def complete(
        self,
        system_blocks: list[str],
        messages: list[dict],
        *,
        effort: str | None = None,
        log_name: str | None = None,
    ) -> Completion:
        req = self.build_request(system_blocks, messages, effort)
        attempt = 0
        while True:
            attempt += 1
            try:
                completion = self._call(req)
                break
            except RetryableAgentError as e:
                if attempt > self.settings.client_retries:
                    raise
                delay = min(2.0 * 2**attempt + random.uniform(0, 1), 30.0)
                log.warning("LLM call failed (%s); retrying in %.0fs", e, delay)
                time.sleep(delay)
        self._log_transcript(req, completion, log_name)
        return completion

    def _call(self, req: dict[str, Any]) -> Completion:
        import anthropic

        client = self._anthropic().with_options(timeout=self.settings.timeout_seconds)
        t0 = time.monotonic()
        try:
            api = client.beta.messages if "betas" in req else client.messages
            with api.stream(**req) as stream:
                response = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise FatalAgentError(
                "Anthropic authentication failed. Run `ant auth login` or export "
                f"ANTHROPIC_API_KEY. ({e.message})"
            ) from e
        except anthropic.PermissionDeniedError as e:
            raise FatalAgentError(f"Anthropic permission denied: {e.message}") from e
        except anthropic.BadRequestError as e:
            raise FatalAgentError(f"Anthropic rejected the request: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise FatalAgentError(
                f"Model or endpoint not found ({self.settings.model}): {e.message}"
            ) from e
        except anthropic.RateLimitError as e:
            raise RetryableAgentError(f"rate limited: {e.message}") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise RetryableAgentError(f"server error {e.status_code}: {e.message}") from e
            raise FatalAgentError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise RetryableAgentError(f"connection error: {e}") from e
        wall = time.monotonic() - t0

        usage = getattr(response, "usage", None)
        model_served = getattr(response, "model", "") or self.settings.model
        cost = cost_from_usage(
            model_served or self.settings.model,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            wall_seconds=wall,
        )
        stop_reason = getattr(response, "stop_reason", None)
        request_id = getattr(response, "_request_id", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            reason = ""
            if details is not None:
                cat = getattr(details, "category", None)
                expl = getattr(details, "explanation", None)
                reason = " ".join(str(x) for x in (cat, expl) if x)
            log.warning("Model refused the request (%s) request_id=%s", reason, request_id)
            return Completion(
                text="",
                content_blocks=[],
                cost=cost,
                request_id=request_id,
                stop_reason=stop_reason,
                refused=True,
                refusal_reason=reason,
                model_served=model_served,
            )
        blocks = list(getattr(response, "content", []) or [])
        log.debug("LLM ok request_id=%s stop=%s usd=%.4f", request_id, stop_reason, cost.usd)
        return Completion(
            text=_text_of(blocks),
            content_blocks=blocks,
            cost=cost,
            request_id=request_id,
            stop_reason=stop_reason,
            model_served=model_served,
        )

    # -- transcripts --------------------------------------------------------------

    def _log_transcript(self, req: dict[str, Any], c: Completion, log_name: str | None) -> None:
        if self.log_dir is None:
            return
        d = self.log_dir / "llm" / (self.run_id or "adhoc")
        d.mkdir(parents=True, exist_ok=True)
        name = log_name or f"call-{int(time.time() * 1000)}"
        record = {
            "at": now_iso(),
            "model": req["model"],
            "model_served": c.model_served,
            "effort": req["output_config"]["effort"],
            "fallbacks": bool(req.get("fallbacks")),
            "system_block_hashes": [_short_hash(b["text"]) for b in req["system"]],
            "messages": [_message_summary(m) for m in req["messages"]],
            "response_text": c.text,
            "stop_reason": c.stop_reason,
            "refused": c.refused,
            "refusal_reason": c.refusal_reason,
            "request_id": c.request_id,
            "usage": {
                "input_tokens": c.cost.input_tokens,
                "output_tokens": c.cost.output_tokens,
                "cache_read_tokens": c.cost.cache_read_tokens,
                "cache_write_tokens": c.cost.cache_write_tokens,
            },
            "usd": c.cost.usd,
            "wall_seconds": c.cost.wall_seconds,
        }
        (d / f"{name}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def _short_hash(text: str) -> str:
    from fver.core.models import sha256_text

    return sha256_text(text)[:12]


def _message_summary(m: dict) -> dict:
    content = m.get("content")
    if isinstance(content, str):
        return {"role": m["role"], "text": content}
    texts = []
    for b in content or []:
        t = getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")
        if t == "text":
            texts.append(b.text if not isinstance(b, dict) else b.get("text", ""))
        else:
            texts.append(f"<{t} block>")
    return {"role": m["role"], "text": "\n".join(texts)}


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeLLMClient:
    """Scripted responses for tests. Same interface as LLMClient.complete."""

    responses: list[str] = field(default_factory=list)
    usd_per_call: float = 0.01
    calls: list[dict[str, Any]] = field(default_factory=list)
    settings: LLMSettings = field(default_factory=LLMSettings)
    refuse_at: set[int] = field(default_factory=set)  # 1-based call indices to refuse

    def complete(
        self,
        system_blocks: list[str],
        messages: list[dict],
        *,
        effort: str | None = None,
        log_name: str | None = None,
    ) -> Completion:
        self.calls.append(
            {"system": list(system_blocks), "messages": list(messages), "effort": effort}
        )
        n = len(self.calls)
        cost = Cost(input_tokens=1000, output_tokens=200, usd=self.usd_per_call, llm_calls=1)
        if n in self.refuse_at:
            return Completion("", [], cost, stop_reason="refusal", refused=True)
        if not self.responses:
            raise FatalAgentError("FakeLLMClient: no scripted responses left")
        text = self.responses.pop(0)
        return Completion(
            text, [_TextBlock(text)], cost, request_id=f"fake-{n}", stop_reason="end_turn"
        )
