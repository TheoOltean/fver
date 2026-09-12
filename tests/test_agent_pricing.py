import pytest

from fver.prove.pricing import cost_from_usage, price_for, usd_for


def test_fable_prices():
    p, known = price_for("claude-fable-5-1")
    assert known and p.input == 10.0 and p.output == 50.0
    assert p.cache_read_rate == 0.25
    assert p.cache_write_rate == pytest.approx(12.5)


def test_prefix_matching_prefers_longest():
    assert price_for("claude-opus-4-8")[0].input == 5.0
    assert price_for("claude-sonnet-4-6")[0].input == 3.0
    assert price_for("claude-sonnet-5")[0].input == 2.0


def test_unknown_model_flagged():
    p, known = price_for("claude-future-9")
    assert not known and p.input == 5.0


def test_usd_math():
    usd = usd_for("claude-opus-5", 1_000_000, 100_000, 500_000, 200_000)
    # 5 + 2.5 + 0.25 + 1.25
    assert usd == pytest.approx(9.0)


def test_cost_from_usage():
    c = cost_from_usage("claude-haiku-4-5", 2000, 1000)
    assert c.llm_calls == 1
    assert c.usd == pytest.approx(0.002 + 0.005)
