from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pytest

from config import Settings
from exchange import MarketLimits, OrderResult
from main import FatalExecutionError, TradingBot, _as_float, _ticker_price
from risk_manager import Position, RiskDecision, RiskManager
from strategy import Signal, SignalAction


def make_bot(tmp_path: Path) -> TradingBot:
    return TradingBot(
        Settings(data_dir=tmp_path, log_dir=tmp_path, dry_run=True)
    )


def make_order(**overrides: object) -> OrderResult:
    values: dict[str, object] = {
        "id": "stop-1",
        "client_order_id": "KRBSTOP",
        "symbol": "BTC/USDT",
        "side": "sell",
        "type": "stop_loss",
        "status": "open",
        "amount": 0.1,
        "filled": 0.0,
        "remaining": 0.1,
        "price": None,
        "average": None,
        "cost": 0.0,
        "fee_quote": 0.0,
        "fee_base": 0.0,
        "raw": {},
    }
    values.update(overrides)
    return OrderResult(**values)


def test_as_float_rejects_nan_and_infinity() -> None:
    assert _as_float(float("nan"), 7.0) == 7.0
    assert _as_float(float("inf"), 7.0) == 7.0
    assert _as_float("12.5") == 12.5
    assert _as_float(None, 3.0) == 3.0


def test_ticker_price_prefers_last() -> None:
    assert _ticker_price({"last": 100.0, "bid": 99.0, "ask": 101.0}) == 100.0
    assert _ticker_price({"bid": 99.0, "ask": 101.0}) == 99.0
    assert _ticker_price(None) == 0.0


def test_protective_stop_requires_open_full_size(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    try:
        assert bot._stop_order_is_protective(make_order(), 0.1)
        assert not bot._stop_order_is_protective(make_order(amount=0.05), 0.1)
        assert not bot._stop_order_is_protective(
            make_order(filled=0.01), 0.1
        )
        assert not bot._stop_order_is_protective(
            make_order(status="canceled"), 0.1
        )
    finally:
        asyncio.run(bot.exchange.close())


def test_exchange_includes_symbols_kept_only_in_state(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_dir=tmp_path,
        dry_run=True,
        symbols=("ETH/USDT",),
    )
    manager = RiskManager(settings)
    manager.register_entry(
        Position(
            symbol="BTC/USDT",
            amount=0.1,
            entry_price=100.0,
            entry_notional=10.0,
            stop_price=98.0,
            initial_stop_price=98.0,
            take_profit_price=104.0,
            highest_price=100.0,
            entry_order_id="entry-1",
            stop_order_id="stop-1",
            entry_fee_quote=0.01,
            opened_at=1_700_000_000.0,
        )
    )
    bot = TradingBot(settings)
    try:
        assert bot.exchange.settings.symbols == ("ETH/USDT", "BTC/USDT")
        assert settings.symbols == ("ETH/USDT",)
    finally:
        asyncio.run(bot.exchange.close())


def test_protective_stop_allows_one_lot_step_short(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    try:
        bot.exchange._limits["BTC/USDT"] = MarketLimits(
            symbol="BTC/USDT",
            min_amount=0.001,
            min_cost=5.0,
            amount_step=0.001,
            price_tick=0.01,
        )
        assert bot._stop_order_is_protective(make_order(amount=0.099), 0.1)
        assert not bot._stop_order_is_protective(
            make_order(amount=0.098), 0.1
        )
    finally:
        asyncio.run(bot.exchange.close())


def test_request_shutdown_sets_event(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    try:
        bot._request_shutdown(signal.SIGTERM)
        assert bot._stop_event.is_set()
    finally:
        asyncio.run(bot.exchange.close())


def test_sleep_until_next_cycle_returns_on_shutdown(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    try:

        async def scenario() -> None:
            bot._stop_event.set()
            await bot._sleep_until_next_cycle()

        asyncio.run(scenario())
    finally:
        asyncio.run(bot.exchange.close())


def make_signal() -> Signal:
    return Signal(
        symbol="BTC/USDT",
        action=SignalAction.BUY,
        price=100.0,
        score=5,
        max_score=7,
        stop_distance_pct=2.0,
        tp_rr=2.0,
        atr_pct=1.5,
        reasons=("teste",),
        generated_at=1_700_000_000.0,
    )


def make_decision() -> RiskDecision:
    return RiskDecision(
        approved=True,
        quote_amount=10.0,
        stop_distance_pct=2.0,
        take_profit_rr=2.0,
        risk_quote=0.22,
        reference_price=100.0,
    )


def test_execute_entry_uses_net_amount_after_base_fee(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    captured: dict[str, float] = {}
    try:
        buy = make_order(
            id="buy-1",
            side="buy",
            type="market",
            amount=0.1,
            filled=0.1,
            remaining=0.0,
            price=100.0,
            average=100.0,
            cost=10.0,
            fee_quote=0.11,
            fee_base=0.001,
        )
        stop = make_order(id="stop-1", amount=0.099, remaining=0.099)

        async def fake_buy(symbol: str, quote: float) -> OrderResult:
            return buy

        async def fake_stop(
            symbol: str, amount: float, price: float
        ) -> OrderResult:
            captured["amount"] = amount
            captured["price"] = price
            return stop

        bot.exchange.create_market_buy = fake_buy  # type: ignore[method-assign]
        bot.exchange.place_stop_loss = fake_stop  # type: ignore[method-assign]
        bot.exchange.floor_amount = lambda symbol, amount: amount  # type: ignore[method-assign]
        bot.exchange.round_price = lambda symbol, price: round(price, 2)  # type: ignore[method-assign]
        bot.alerter.notify = lambda message: None  # type: ignore[method-assign]

        position = asyncio.run(
            bot._execute_entry(
                make_signal(), make_decision(), datetime.now(timezone.utc)
            )
        )

        assert position is not None
        assert position.amount == pytest.approx(0.099)
        assert position.entry_fee_quote == pytest.approx(0.11)
        assert position.stop_order_id == "stop-1"
        assert captured["amount"] == pytest.approx(0.099)
        assert captured["price"] == pytest.approx(98.0)
    finally:
        asyncio.run(bot.exchange.close())


def test_replace_stop_keeps_old_stop_when_cancel_is_uncertain(
    tmp_path: Path,
) -> None:
    bot = make_bot(tmp_path)
    placed: list[float] = []
    try:
        position = Position(
            symbol="BTC/USDT",
            amount=0.1,
            entry_price=100.0,
            entry_notional=10.0,
            stop_price=98.0,
            initial_stop_price=98.0,
            take_profit_price=104.0,
            highest_price=101.0,
            entry_order_id="entry-1",
            stop_order_id="old-stop",
            entry_fee_quote=0.01,
            opened_at=1_700_000_000.0,
        )
        bot.risk.register_entry(position)
        old_stop = make_order(id="old-stop", amount=0.1, remaining=0.1)

        async def failing_cancel(symbol: str, order_id: str) -> bool:
            raise ccxt.NetworkError("timeout no cancelamento")

        async def fake_fetch(
            symbol: str, order_id: str | None = None
        ) -> OrderResult:
            return old_stop

        async def fake_place(
            symbol: str, amount: float, price: float
        ) -> OrderResult:
            placed.append(price)
            return make_order(id="new-stop", amount=0.1, remaining=0.1)

        bot.exchange.cancel_order = failing_cancel  # type: ignore[method-assign]
        bot.exchange.fetch_order = fake_fetch  # type: ignore[method-assign]
        bot.exchange.place_stop_loss = fake_place  # type: ignore[method-assign]
        bot.exchange.round_price = lambda symbol, price: round(price, 2)  # type: ignore[method-assign]

        asyncio.run(
            bot._replace_stop(
                position, 99.0, 101.0, datetime.now(timezone.utc)
            )
        )

        assert placed == []
        assert position.stop_order_id == "old-stop"
        assert position.stop_price == pytest.approx(98.0)
    finally:
        asyncio.run(bot.exchange.close())


def test_replace_stop_fails_closed_when_state_is_unknown(
    tmp_path: Path,
) -> None:
    bot = make_bot(tmp_path)
    try:
        position = Position(
            symbol="BTC/USDT",
            amount=0.1,
            entry_price=100.0,
            entry_notional=10.0,
            stop_price=98.0,
            initial_stop_price=98.0,
            take_profit_price=104.0,
            highest_price=101.0,
            entry_order_id="entry-1",
            stop_order_id="old-stop",
            entry_fee_quote=0.01,
            opened_at=1_700_000_000.0,
        )
        bot.risk.register_entry(position)

        async def failing_cancel(symbol: str, order_id: str) -> bool:
            raise ccxt.NetworkError("timeout no cancelamento")

        async def failing_fetch(
            symbol: str, order_id: str | None = None
        ) -> OrderResult:
            raise ccxt.NetworkError("timeout na consulta")

        bot.exchange.cancel_order = failing_cancel  # type: ignore[method-assign]
        bot.exchange.fetch_order = failing_fetch  # type: ignore[method-assign]

        with pytest.raises(FatalExecutionError):
            asyncio.run(
                bot._replace_stop(
                    position, 99.0, 101.0, datetime.now(timezone.utc)
                )
            )
    finally:
        asyncio.run(bot.exchange.close())


def test_compute_equity_marks_open_position_to_market(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    try:
        bot.risk.register_entry(
            Position(
                symbol="BTC/USDT",
                amount=0.1,
                entry_price=100.0,
                entry_notional=10.0,
                stop_price=98.0,
                initial_stop_price=98.0,
                take_profit_price=104.0,
                highest_price=100.0,
                entry_order_id="entry-1",
                stop_order_id="stop-1",
                entry_fee_quote=0.01,
                opened_at=1_700_000_000.0,
            )
        )
        equity = bot._compute_equity(
            {"total": {"USDT": 50.0}},
            {"BTC/USDT": {"last": 120.0}},
        )
        assert equity == 62.0
    finally:
        asyncio.run(bot.exchange.close())
