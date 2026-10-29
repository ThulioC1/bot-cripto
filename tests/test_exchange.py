from __future__ import annotations

import asyncio
from pathlib import Path

import ccxt

from config import Settings
from exchange import ExchangeClient


def make_client(tmp_path: Path) -> ExchangeClient:
    return ExchangeClient(
        Settings(data_dir=tmp_path, log_dir=tmp_path, dry_run=True)
    )


def test_parse_order_normalizes_status_and_fee(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    try:
        order = client._parse_order(
            {
                "id": "123",
                "clientOrderId": "KRBSELL",
                "symbol": "BTC/USDT",
                "side": "sell",
                "type": "stop_loss",
                "status": "filled",
                "amount": 1.0,
                "filled": 1.0,
                "remaining": 0.0,
                "price": None,
                "average": 100.0,
                "cost": 100.0,
                "fee": {"currency": "USDT", "cost": 0.1},
            },
            "BTC/USDT",
        )
    finally:
        asyncio.run(client.close())

    assert order.id == "123"
    assert order.is_closed
    assert not order.is_open
    assert order.fee_quote == 0.1
    assert order.fee_base == 0.0
    assert order.cost == 100.0


def test_parse_order_converts_base_currency_fee(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    try:
        order = client._parse_order(
            {
                "id": "124",
                "symbol": "BTC/USDT",
                "side": "buy",
                "type": "market",
                "status": "closed",
                "amount": 1.0,
                "filled": 1.0,
                "remaining": 0.0,
                "average": 100.0,
                "cost": 100.0,
                "fees": [{"currency": "BTC", "cost": 0.001}],
            },
            "BTC/USDT",
        )
    finally:
        asyncio.run(client.close())

    assert order.fee_quote == 0.1
    assert order.fee_base == 0.001


def test_parse_order_reads_fee_from_ccxt_trades(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    try:
        order = client._parse_order(
            {
                "id": "126",
                "symbol": "BTC/USDT",
                "side": "buy",
                "type": "market",
                "status": "closed",
                "amount": 1.0,
                "filled": 1.0,
                "remaining": 0.0,
                "average": 100.0,
                "cost": 100.0,
                "fee": None,
                "fees": [],
                "trades": [
                    {
                        "amount": 1.0,
                        "price": 100.0,
                        "cost": 100.0,
                        "fee": {"currency": "BTC", "cost": 0.001},
                        "info": {"commission": "0.001", "commissionAsset": "BTC"},
                    }
                ],
            },
            "BTC/USDT",
        )
    finally:
        asyncio.run(client.close())

    assert order.fee_quote == 0.1
    assert order.fee_base == 0.001


def test_parse_order_falls_back_to_configured_fee_rate(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    try:
        order = client._parse_order(
            {
                "id": "125",
                "symbol": "BTC/USDT",
                "side": "buy",
                "type": "market",
                "status": "closed",
                "amount": 1.0,
                "filled": 1.0,
                "remaining": 0.0,
                "average": 100.0,
                "cost": 100.0,
            },
            "BTC/USDT",
        )
    finally:
        asyncio.run(client.close())

    assert order.fee_quote == 0.1


def test_build_limits_reads_binance_tick_size(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    try:
        assert client.raw.precisionMode == ccxt.TICK_SIZE
        limits = client._build_limits(
            {
                "symbol": "BTC/USDT",
                "limits": {
                    "amount": {"min": 0.00001},
                    "cost": {"min": 5.0},
                },
                "precision": {"amount": 0.00001, "price": 0.01},
            }
        )
    finally:
        asyncio.run(client.close())

    assert limits.min_amount == 0.00001
    assert limits.min_cost == 5.0
    assert limits.amount_step == 0.00001
    assert limits.price_tick == 0.01


def test_balance_helpers_handle_missing_sections(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    try:
        balance = {"free": {"USDT": "12.5"}, "total": {"USDT": 20}}
        assert client.balance_free(balance, "USDT") == 12.5
        assert client.balance_total(balance, "USDT") == 20.0
        assert client.balance_free({}, "USDT") == 0.0
    finally:
        asyncio.run(client.close())
