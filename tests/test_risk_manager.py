from __future__ import annotations

import math
from pathlib import Path

import pytest

from config import Settings
from risk_manager import Position, RiskManager, StateStoreError


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "log_dir": tmp_path,
        "dry_run": True,
    }
    values.update(overrides)
    return Settings(**values)


def make_position(**overrides: object) -> Position:
    values: dict[str, object] = {
        "symbol": "BTC/USDT",
        "amount": 0.1,
        "entry_price": 100.0,
        "entry_notional": 10.0,
        "stop_price": 98.0,
        "initial_stop_price": 98.0,
        "take_profit_price": 104.0,
        "highest_price": 100.0,
        "entry_order_id": "entry-1",
        "stop_order_id": "stop-1",
        "entry_fee_quote": 0.01,
        "opened_at": 1_700_000_000.0,
    }
    values.update(overrides)
    return Position(**values)


def test_size_trade_respects_position_cap(tmp_path: Path) -> None:
    manager = RiskManager(make_settings(tmp_path), tmp_path / "state.json")
    decision = manager.size_trade(
        symbol="BTC/USDT",
        equity=1_000.0,
        free_quote=1_000.0,
        stop_distance_pct=2.0,
        tp_rr=2.0,
        reference_price=100.0,
        min_notional=5.0,
        now=1_700_000_000.0,
    )
    assert decision.approved
    assert decision.quote_amount == pytest.approx(50.0)
    assert decision.stop_price == pytest.approx(98.0)
    assert decision.take_profit_price == pytest.approx(104.0)


def test_size_trade_blocks_when_reserve_leaves_too_little(tmp_path: Path) -> None:
    manager = RiskManager(make_settings(tmp_path), tmp_path / "state.json")
    decision = manager.size_trade(
        symbol="BTC/USDT",
        equity=1_000.0,
        free_quote=195.0,
        stop_distance_pct=2.0,
        tp_rr=2.0,
        reference_price=100.0,
        min_notional=5.0,
    )
    assert not decision.approved
    assert "reserva" in decision.reason


def test_size_trade_reserves_cash_for_fee(tmp_path: Path) -> None:
    manager = RiskManager(make_settings(tmp_path), tmp_path / "state.json")
    decision = manager.size_trade(
        symbol="BTC/USDT",
        equity=1_000.0,
        free_quote=240.0,
        stop_distance_pct=2.0,
        tp_rr=2.0,
        reference_price=100.0,
        min_notional=5.0,
    )
    assert decision.approved
    assert decision.quote_amount == pytest.approx(39.96)


def test_size_trade_blocks_below_exchange_minimum(tmp_path: Path) -> None:
    manager = RiskManager(make_settings(tmp_path), tmp_path / "state.json")
    decision = manager.size_trade(
        symbol="BTC/USDT",
        equity=100.0,
        free_quote=100.0,
        stop_distance_pct=2.0,
        tp_rr=2.0,
        reference_price=100.0,
        min_notional=10.0,
    )
    assert not decision.approved
    assert "abaixo do minimo" in decision.reason


def test_circuit_breaker_trips_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    manager = RiskManager(make_settings(tmp_path), path)
    manager.record_equity(1_000.0, 1_700_000_000.0)
    report = manager.record_equity(960.0, 1_700_000_100.0)
    assert report.tripped_now
    assert report.drawdown_pct == pytest.approx(4.0)
    assert manager.halted
    assert path.exists()

    reloaded = RiskManager(make_settings(tmp_path), path)
    assert reloaded.halted
    assert "4.00%" in reloaded.state.halt_reason

    reloaded.reset_halt()
    assert not reloaded.halted
    assert reloaded.state.equity_history == []


def test_backup_is_used_when_primary_state_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    manager = RiskManager(make_settings(tmp_path), path)
    manager.state.halted = True
    manager.state.halt_reason = "teste"
    manager.save()
    assert path.with_name("state.json.bak").exists()
    path.unlink()

    restored = RiskManager(make_settings(tmp_path), path)
    assert restored.halted
    assert restored.state.halt_reason == "teste"
    assert path.exists()


def test_position_from_dict_rejects_inconsistent_levels() -> None:
    payload = make_position().to_dict()
    payload["stop_price"] = 110.0
    with pytest.raises(ValueError, match="stop"):
        Position.from_dict(payload)

    payload = make_position().to_dict()
    payload["initial_stop_price"] = 105.0
    with pytest.raises(ValueError, match="inicial"):
        Position.from_dict(payload)


def test_record_close_calculates_pnl_and_cooldown(tmp_path: Path) -> None:
    manager = RiskManager(make_settings(tmp_path), tmp_path / "state.json")
    manager.register_entry(make_position())
    close = manager.record_close(
        "BTC/USDT",
        90.0,
        "stop_loss",
        exit_fee_quote=0.01,
        now=1_700_000_100.0,
    )
    assert close.pnl_quote == pytest.approx(-1.02)
    assert close.pnl_pct == pytest.approx(-10.2)
    assert "BTC/USDT" not in manager.positions
    assert manager.symbol_cooldown_remaining("BTC/USDT", 1_700_000_100.0) > 0
    assert manager.state.trades[-1]["reason"] == "stop_loss"


def test_trailing_stop_never_moves_down(tmp_path: Path) -> None:
    manager = RiskManager(make_settings(tmp_path), tmp_path / "state.json")
    position = make_position(
        stop_price=105.0, initial_stop_price=98.0, highest_price=110.0
    )
    proposed = manager.proposed_trailing_stop(position, 110.0, 1.0)
    assert proposed is not None
    assert proposed > position.stop_price
    assert proposed < 110.0

    proposed_lower = manager.proposed_trailing_stop(position, 105.0, 3.0)
    assert proposed_lower is None


def test_corrupted_state_without_backup_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{invalid", encoding="utf-8")
    with pytest.raises(StateStoreError):
        RiskManager(make_settings(tmp_path), path)


def test_trade_limits_and_invalid_inputs(tmp_path: Path) -> None:
    manager = RiskManager(
        make_settings(tmp_path, max_open_positions=1), tmp_path / "state.json"
    )
    manager.register_entry(make_position())
    decision = manager.size_trade(
        symbol="ETH/USDT",
        equity=1_000.0,
        free_quote=1_000.0,
        stop_distance_pct=2.0,
        tp_rr=2.0,
        reference_price=100.0,
        min_notional=5.0,
    )
    assert not decision.approved
    assert "simultaneas" in decision.reason

    with pytest.raises(ValueError):
        manager.record_equity(0.0)
    with pytest.raises(ValueError):
        manager.record_equity(math.nan)
