"""Token pricing per model, used for cost accounting and budgets.

Prices are USD per 1M tokens. Cache reads are 10% of the input price except
where a model publishes its own rate; cache writes are 125% of input.
"""

from __future__ import annotations

from dataclasses import dataclass

from fver.core.models import Cost


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float | None = None  # None -> 10% of input
    cache_write: float | None = None  # None -> 125% of input

    @property
    def cache_read_rate(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * 0.10

    @property
    def cache_write_rate(self) -> float:
        return self.cache_write if self.cache_write is not None else self.input * 1.25


# Longest prefixes first so "claude-opus-4-8" does not match "claude-opus-4".
PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, cache_read=0.25),
    "claude-fable-5": Price(10.0, 50.0),
    "claude-mythos-5-1": Price(10.0, 50.0, cache_read=0.25),
    "claude-opus-5": Price(5.0, 25.0),
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-opus-4-7": Price(5.0, 25.0),
    "claude-opus-4-6": Price(5.0, 25.0),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-sonnet-4-6": Price(3.0, 15.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
}

FALLBACK_PRICE = Price(5.0, 25.0)  # opus-tier estimate for unknown models


def price_for(model: str) -> tuple[Price, bool]:
    """Return (price, known). Unknown models get the opus estimate."""
    for prefix in sorted(PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            return PRICES[prefix], True
    return FALLBACK_PRICE, False


def usd_for(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    p, _ = price_for(model)
    per = 1_000_000
    return (
        input_tokens * p.input / per
        + output_tokens * p.output / per
        + cache_read_tokens * p.cache_read_rate / per
        + cache_write_tokens * p.cache_write_rate / per
    )


def cost_from_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    wall_seconds: float = 0.0,
) -> Cost:
    return Cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        usd=usd_for(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens),
        wall_seconds=wall_seconds,
        llm_calls=1,
    )


def estimate_attempt_usd(
    model: str, input_tokens: int = 25_000, output_tokens: int = 6_000
) -> float:
    """Rough per-attempt estimate for `verify --dry-run`."""
    return usd_for(model, input_tokens, output_tokens)
