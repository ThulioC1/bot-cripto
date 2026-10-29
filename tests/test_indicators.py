from __future__ import annotations

import pytest

from strategy import (
    atr,
    bollinger,
    closed_candles,
    ema,
    macd,
    rsi,
    sma,
)


def test_sma_basic() -> None:
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_sma_short_series() -> None:
    assert sma([1, 2], 3) == [None, None]


def test_ema_seed_and_recursion() -> None:
    assert ema([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]


def test_rsi_all_gains_and_losses() -> None:
    assert rsi([1, 2, 3, 4, 5], 2) == [None, None, 100.0, 100.0, 100.0]
    assert rsi([5, 4, 3, 2, 1], 2) == [None, None, 0.0, 0.0, 0.0]


def test_atr_constant_range() -> None:
    series = atr([10, 10, 10, 10, 10], [8, 8, 8, 8, 8], [9, 9, 9, 9, 9], 3)
    assert series == [None, None, 2.0, 2.0, 2.0]


def test_bollinger_constant_series() -> None:
    upper, middle, lower = bollinger([100.0] * 20, 20, 2.0)
    assert upper[-1] == pytest.approx(100.0)
    assert middle[-1] == pytest.approx(100.0)
    assert lower[-1] == pytest.approx(100.0)


def test_macd_has_values_after_slow_ema() -> None:
    closes = [float(value) for value in range(1, 101)]
    macd_line, signal, histogram = macd(closes, 12, 26, 9)
    assert macd_line[-1] is not None
    assert signal[-1] is not None
    assert histogram[-1] is not None
    assert len(macd_line) == len(closes)


def test_closed_candles_excludes_current_candle() -> None:
    candles = [
        [1000, 1, 2, 1, 1.5, 10],
        [2000, 1, 2, 1, 1.5, 10],
        [3000, 1, 2, 1, 1.5, 10],
        [4000, 1, 2, 1, 1.5, 10],
        [5000, 1, 2, 1, 1.5, 10],
    ]
    assert len(closed_candles(candles, 5000, 1000)) == 4


def test_closed_candles_ignores_malformed_rows() -> None:
    candles = [[1000, 1, 2], [2000, 1, 2, 1, 1.5, 10]]
    assert len(closed_candles(candles, 5000, 1000)) == 1


def test_indicators_reject_invalid_period() -> None:
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)
    with pytest.raises(ValueError):
        ema([1, 2, 3], 0)
    with pytest.raises(ValueError):
        rsi([1, 2, 3], 0)
    with pytest.raises(ValueError):
        atr([1, 2, 3], [1, 2, 3], [1, 2, 3], 0)
