from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from config import Settings

Number = float
OptionalSeries = list[float | None]


class SignalAction(str, Enum):
    BUY = "buy"
    HOLD = "hold"


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: SignalAction
    price: float
    score: int
    max_score: int
    stop_distance_pct: float
    tp_rr: float
    atr_pct: float
    reasons: tuple[str, ...]
    generated_at: float

    @property
    def is_buy(self) -> bool:
        return self.action is SignalAction.BUY

    @property
    def confidence(self) -> float:
        return self.score / self.max_score if self.max_score else 0.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def sma(values: Sequence[float], period: int) -> OptionalSeries:
    if period <= 0:
        raise ValueError("period deve ser positivo.")
    out: OptionalSeries = [None] * len(values)
    if len(values) < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for index in range(period, len(values)):
        window_sum += values[index] - values[index - period]
        out[index] = window_sum / period
    return out


def ema(values: Sequence[float], period: int) -> OptionalSeries:
    if period <= 0:
        raise ValueError("period deve ser positivo.")
    out: OptionalSeries = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    multiplier = 2.0 / (period + 1.0)
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * multiplier + previous * (1.0 - multiplier)
        out[index] = previous
    return out


def rsi(values: Sequence[float], period: int = 14) -> OptionalSeries:
    if period <= 0:
        raise ValueError("period deve ser positivo.")
    out: OptionalSeries = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        delta = values[index] - values[index - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    average_gain = gains / period
    average_loss = losses / period
    out[period] = _rsi_value(average_gain, average_loss)
    for index in range(period + 1, len(values)):
        delta = values[index] - values[index - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        out[index] = _rsi_value(average_gain, average_loss)
    return out


def _rsi_value(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def macd(
    values: Sequence[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[OptionalSeries, OptionalSeries, OptionalSeries]:
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)
    macd_line: OptionalSeries = [None] * len(values)
    start = None
    for index in range(len(values)):
        if fast[index] is not None and slow[index] is not None:
            macd_line[index] = fast[index] - slow[index]
            if start is None:
                start = index
    signal_line: OptionalSeries = [None] * len(values)
    histogram: OptionalSeries = [None] * len(values)
    if start is None:
        return macd_line, signal_line, histogram
    compact = [value for value in macd_line[start:] if value is not None]
    signal_compact = ema(compact, signal_period)
    for offset, signal_value in enumerate(signal_compact):
        if signal_value is None:
            continue
        index = start + offset
        signal_line[index] = signal_value
        if macd_line[index] is not None:
            histogram[index] = macd_line[index] - signal_value
    return macd_line, signal_line, histogram


def bollinger(
    values: Sequence[float], period: int = 20, std_multiplier: float = 2.0
) -> tuple[OptionalSeries, OptionalSeries, OptionalSeries]:
    middle = sma(values, period)
    upper: OptionalSeries = [None] * len(values)
    lower: OptionalSeries = [None] * len(values)
    for index, mean in enumerate(middle):
        if mean is None or index + 1 < period:
            continue
        window = values[index + 1 - period : index + 1]
        variance = sum((value - mean) ** 2 for value in window) / period
        deviation = math.sqrt(max(variance, 0.0))
        upper[index] = mean + std_multiplier * deviation
        lower[index] = mean - std_multiplier * deviation
    return upper, middle, lower


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> OptionalSeries:
    if period <= 0:
        raise ValueError("period deve ser positivo.")
    length = min(len(highs), len(lows), len(closes))
    out: OptionalSeries = [None] * length
    if length < period:
        return out
    true_ranges: list[float] = []
    for index in range(length):
        high = highs[index]
        low = lows[index]
        if index == 0:
            true_ranges.append(high - low)
        else:
            previous_close = closes[index - 1]
            true_ranges.append(
                max(
                    high - low,
                    abs(high - previous_close),
                    abs(low - previous_close),
                )
            )
    seed = sum(true_ranges[:period]) / period
    out[period - 1] = seed
    previous = seed
    for index in range(period, length):
        previous = (previous * (period - 1) + true_ranges[index]) / period
        out[index] = previous
    return out


def closed_candles(
    candles: Sequence[Sequence[float]], now_ms: int, timeframe_ms: int
) -> list[Sequence[float]]:
    if not candles:
        return []
    closed = [
        candle
        for candle in candles
        if len(candle) >= 6 and _safe_float(candle[0]) + timeframe_ms <= now_ms
    ]
    if closed:
        return closed
    return list(candles[:-1])


class Strategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def latest_atr(
        self, candles: Sequence[Sequence[float]], now: datetime
    ) -> float | None:
        now_ms = int(now.timestamp() * 1000)
        closed = closed_candles(candles, now_ms, self.settings.timeframe_ms)
        if len(closed) < self.settings.atr_period + 1:
            return None
        highs = [_safe_float(row[2]) for row in closed]
        lows = [_safe_float(row[3]) for row in closed]
        closes = [_safe_float(row[4]) for row in closed]
        series = atr(highs, lows, closes, self.settings.atr_period)
        return series[-1]

    def evaluate(
        self,
        symbol: str,
        candles: Sequence[Sequence[float]],
        ticker: Mapping[str, Any],
        now: datetime | None = None,
    ) -> Signal:
        now = now or datetime.now(timezone.utc)
        settings = self.settings
        now_ms = int(now.timestamp() * 1000)
        rows = closed_candles(candles, now_ms, settings.timeframe_ms)
        required = max(
            settings.ema_slow + 10,
            settings.bb_period + 5,
            settings.macd_slow + settings.macd_signal + 5,
            settings.atr_period + 5,
            settings.volume_ma_period + 5,
            settings.breakout_lookback + 2,
            60,
        )
        if len(rows) < required:
            return self._hold(
                symbol,
                0.0,
                f"candles insuficientes ({len(rows)}/{required})",
                now,
            )

        highs = [_safe_float(row[2]) for row in rows]
        lows = [_safe_float(row[3]) for row in rows]
        closes = [_safe_float(row[4]) for row in rows]
        volumes = [_safe_float(row[5]) for row in rows]
        close = closes[-1]
        if close <= 0:
            return self._hold(symbol, close, "preco de fechamento invalido", now)

        ema_fast_series = ema(closes, settings.ema_fast)
        ema_slow_series = ema(closes, settings.ema_slow)
        rsi_series = rsi(closes, settings.rsi_period)
        macd_line, macd_signal, macd_hist = macd(
            closes,
            settings.macd_fast,
            settings.macd_slow,
            settings.macd_signal,
        )
        bb_upper, bb_middle, _bb_lower = bollinger(
            closes, settings.bb_period, settings.bb_std
        )
        atr_series = atr(highs, lows, closes, settings.atr_period)
        volume_average = sma(volumes, settings.volume_ma_period)

        ema_fast = ema_fast_series[-1]
        ema_slow = ema_slow_series[-1]
        ema_slow_previous = ema_slow_series[-6] if len(ema_slow_series) >= 6 else None
        rsi_value = rsi_series[-1]
        macd_value = macd_line[-1]
        macd_signal_value = macd_signal[-1]
        hist_value = macd_hist[-1]
        hist_previous = macd_hist[-2] if len(macd_hist) >= 2 else None
        bb_middle_value = bb_middle[-1]
        bb_upper_value = bb_upper[-1]
        atr_value = atr_series[-1]
        volume_average_value = volume_average[-1]
        if any(
            value is None
            for value in (
                ema_fast,
                ema_slow,
                ema_slow_previous,
                rsi_value,
                macd_value,
                macd_signal_value,
                hist_value,
                hist_previous,
                bb_middle_value,
                bb_upper_value,
                atr_value,
                volume_average_value,
            )
        ):
            return self._hold(symbol, close, "indicadores ainda incompletos", now)

        assert ema_fast is not None
        assert ema_slow is not None
        assert ema_slow_previous is not None
        assert rsi_value is not None
        assert macd_value is not None
        assert macd_signal_value is not None
        assert hist_value is not None
        assert hist_previous is not None
        assert bb_middle_value is not None
        assert bb_upper_value is not None
        assert atr_value is not None
        assert volume_average_value is not None

        atr_pct = atr_value / close * 100.0
        previous_close = closes[-2] if len(closes) >= 2 else close
        last_candle_gain_pct = (
            (close / previous_close - 1.0) * 100.0 if previous_close > 0 else 0.0
        )

        last_volume = volumes[-1]
        volume_ratio = (
            last_volume / volume_average_value if volume_average_value > 0 else 0.0
        )
        daily_bars = max(1, int(86_400_000 / settings.timeframe_ms))
        lookback = min(daily_bars, len(rows))
        rolling_quote_volume = sum(
            closes[-lookback + index] * volumes[-lookback + index]
            for index in range(lookback)
        )
        ticker_quote_volume = _safe_float(ticker.get("quoteVolume"))
        quote_volume_24h = max(rolling_quote_volume, ticker_quote_volume)

        mandatory_failures: list[str] = []
        if not settings.min_atr_pct <= atr_pct <= settings.max_atr_pct:
            mandatory_failures.append(
                f"ATR% fora da faixa ({atr_pct:.2f}% nao esta entre "
                f"{settings.min_atr_pct:.2f}% e {settings.max_atr_pct:.2f}%)"
            )
        if quote_volume_24h < settings.min_24h_quote_volume:
            mandatory_failures.append(
                f"volume 24h insuficiente ({quote_volume_24h:,.0f} < "
                f"{settings.min_24h_quote_volume:,.0f})"
            )
        if rsi_value > 75.0:
            mandatory_failures.append(f"RSI sobrecomprado ({rsi_value:.1f})")
        if last_candle_gain_pct > settings.max_last_candle_gain_pct:
            mandatory_failures.append(
                f"candle atual esticado (+{last_candle_gain_pct:.2f}%)"
            )
        if ema_fast <= ema_slow:
            mandatory_failures.append("EMA rapida abaixo da lenta (sem tendencia de alta)")
        if ema_slow <= ema_slow_previous:
            mandatory_failures.append("EMA lenta sem inclinacao de alta")
        if close < ema_slow:
            mandatory_failures.append("preco abaixo da EMA lenta")
        if atr_value > 0 and (close - ema_fast) / atr_value > 3.5:
            mandatory_failures.append("preco muito estendido acima da EMA rapida")

        if mandatory_failures:
            return self._hold(symbol, close, "; ".join(mandatory_failures), now)

        recent_high = max(highs[-settings.breakout_lookback - 1 : -1])
        separation = ema_fast - ema_slow
        score = 0
        passed: list[str] = []
        if separation >= 0.25 * atr_value:
            score += 1
            passed.append("tendencia forte")
        if macd_value > macd_signal_value:
            score += 1
            passed.append("MACD acima do sinal")
        if hist_value > 0 and hist_value > hist_previous:
            score += 1
            passed.append("momentum MACD acelerando")
        if settings.rsi_min <= rsi_value <= settings.rsi_max:
            score += 1
            passed.append(f"RSI saudavel ({rsi_value:.1f})")
        if close > bb_middle_value and close <= bb_upper_value:
            score += 1
            passed.append("preco acima da banda media com espaco")
        if volume_ratio >= settings.min_volume_ratio:
            score += 1
            passed.append(f"volume {volume_ratio:.2f}x a media")
        if recent_high > 0 and close >= recent_high * 0.995:
            score += 1
            passed.append("rompimento/proximidade da maxima")

        max_score = 7
        if score < settings.entry_min_score:
            return self._hold(
                symbol,
                close,
                f"score {score}/{max_score} abaixo do minimo "
                f"{settings.entry_min_score} ({', '.join(passed) or 'sem confluencias'})",
                now,
                score=score,
            )

        swing_low = min(lows[-10:])
        atr_stop_pct = settings.stop_atr_mult * atr_value / close * 100.0
        structure_stop_pct = max((close - swing_low) / close * 100.0 + 0.15, 0.0)
        raw_stop_pct = max(atr_stop_pct, structure_stop_pct)
        stop_distance_pct = min(
            max(raw_stop_pct, settings.min_stop_pct), settings.max_stop_pct
        )
        take_profit_pct = stop_distance_pct * settings.tp_rr
        reasons = (
            f"score {score}/{max_score}",
            (
                f"ATR {atr_pct:.2f}% | stop {stop_distance_pct:.2f}% | "
                f"TP {take_profit_pct:.2f}% (RR 1:{settings.tp_rr:.2f})"
            ),
            ", ".join(passed),
        )
        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            price=close,
            score=score,
            max_score=max_score,
            stop_distance_pct=stop_distance_pct,
            tp_rr=settings.tp_rr,
            atr_pct=atr_pct,
            reasons=reasons,
            generated_at=now.timestamp(),
        )

    def _hold(
        self,
        symbol: str,
        price: float,
        reason: str,
        now: datetime,
        score: int = 0,
    ) -> Signal:
        return Signal(
            symbol=symbol,
            action=SignalAction.HOLD,
            price=price,
            score=score,
            max_score=7,
            stop_distance_pct=0.0,
            tp_rr=self.settings.tp_rr,
            atr_pct=0.0,
            reasons=(reason,),
            generated_at=now.timestamp(),
        )
