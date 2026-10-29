from __future__ import annotations

import json
import logging
import math
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Settings

logger = logging.getLogger("bot.risk")

STATE_VERSION = 1
TRADE_HISTORY_LIMIT = 200
EQUITY_HISTORY_LIMIT = 5_000


class StateStoreError(RuntimeError):
    pass


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    timestamp = _number(value)
    return timestamp if timestamp > 0 else None


@dataclass
class Position:
    symbol: str
    amount: float
    entry_price: float
    entry_notional: float
    stop_price: float
    initial_stop_price: float
    take_profit_price: float
    highest_price: float
    entry_order_id: str
    stop_order_id: str | None
    entry_fee_quote: float
    opened_at: float

    @property
    def risk_per_unit(self) -> float:
        return max(self.entry_price - self.initial_stop_price, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "amount": self.amount,
            "entry_price": self.entry_price,
            "entry_notional": self.entry_notional,
            "stop_price": self.stop_price,
            "initial_stop_price": self.initial_stop_price,
            "take_profit_price": self.take_profit_price,
            "highest_price": self.highest_price,
            "entry_order_id": self.entry_order_id,
            "stop_order_id": self.stop_order_id,
            "entry_fee_quote": self.entry_fee_quote,
            "opened_at": self.opened_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Position:
        symbol = str(data.get("symbol") or "")
        amount = _number(data.get("amount"))
        entry_price = _number(data.get("entry_price"))
        stop_price = _number(data.get("stop_price"))
        take_profit_price = _number(data.get("take_profit_price"))
        if not symbol or amount <= 0 or entry_price <= 0:
            raise ValueError("posicao com dados essenciais invalidos")
        if stop_price <= 0 or take_profit_price <= 0:
            raise ValueError("posicao com stop/take-profit invalidos")
        if stop_price >= take_profit_price:
            raise ValueError("stop precisa estar abaixo do take-profit")
        if stop_price >= entry_price * 2 or take_profit_price <= entry_price * 0.5:
            raise ValueError("niveis de stop/take-profit fora de faixa plausivel")
        entry_notional = _number(data.get("entry_notional"), entry_price * amount)
        if entry_notional <= 0:
            entry_notional = entry_price * amount
        initial_stop = _number(data.get("initial_stop_price"), stop_price)
        if initial_stop <= 0 or initial_stop >= entry_price:
            raise ValueError("stop inicial invalido")
        highest = _number(data.get("highest_price"), entry_price)
        entry_fee = max(_number(data.get("entry_fee_quote")), 0.0)
        opened_at = _number(data.get("opened_at"), time.time())
        return cls(
            symbol=symbol,
            amount=amount,
            entry_price=entry_price,
            entry_notional=entry_notional,
            stop_price=stop_price,
            initial_stop_price=initial_stop,
            take_profit_price=take_profit_price,
            highest_price=max(highest, entry_price),
            entry_order_id=str(data.get("entry_order_id") or ""),
            stop_order_id=(
                str(data["stop_order_id"]) if data.get("stop_order_id") else None
            ),
            entry_fee_quote=entry_fee,
            opened_at=opened_at if opened_at > 0 else time.time(),
        )


@dataclass
class BotState:
    version: int = STATE_VERSION
    halted: bool = False
    halt_reason: str = ""
    halted_at: float | None = None
    positions: dict[str, Position] = field(default_factory=dict)
    equity_history: list[dict[str, float]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    trade_day: str = ""
    trades_today: int = 0
    cooldowns: dict[str, float] = field(default_factory=dict)
    last_cycle_at: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    quote_amount: float = 0.0
    stop_distance_pct: float = 0.0
    take_profit_rr: float = 0.0
    risk_quote: float = 0.0
    reference_price: float = 0.0
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class CircuitBreakerReport:
    tripped_now: bool
    halted: bool
    drawdown_pct: float
    peak_equity: float
    limit_pct: float


@dataclass(frozen=True)
class TradeClose:
    symbol: str
    amount: float
    exit_price: float
    pnl_quote: float
    pnl_pct: float
    reason: str
    closed_at: float


class RiskManager:
    def __init__(
        self, settings: Settings, state_path: Path | None = None
    ) -> None:
        self.settings = settings
        self.state_path = state_path or settings.state_path
        self._prepare_directories()
        self.state = self._load_state()
        if self._roll_day(time.time()):
            self._save()

    def _prepare_directories(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                self.state_path.parent.chmod(0o700)
            except OSError:
                pass

    @property
    def halted(self) -> bool:
        return self.state.halted

    @property
    def positions(self) -> dict[str, Position]:
        return self.state.positions

    def open_exposure_quote(self) -> float:
        return sum(
            position.entry_notional for position in self.state.positions.values()
        )

    def symbol_cooldown_remaining(self, symbol: str, now: float | None = None) -> float:
        now = now or time.time()
        until = self.state.cooldowns.get(symbol, 0.0)
        return max(until - now, 0.0)

    def record_equity(
        self, equity: float, now: float | None = None
    ) -> CircuitBreakerReport:
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("Equity invalida para o circuit breaker.")
        now = now or time.time()
        self._roll_day(now)
        cutoff = now - 86_400
        history = [
            entry
            for entry in self.state.equity_history
            if _number(entry.get("ts")) >= cutoff
        ]
        history.append({"ts": now, "equity": equity})
        if len(history) > EQUITY_HISTORY_LIMIT:
            history = history[-EQUITY_HISTORY_LIMIT:]
        self.state.equity_history = history
        peak = max(_number(entry.get("equity")) for entry in history)
        drawdown_pct = (
            max((peak - equity) / peak * 100.0, 0.0) if peak > 0 else 0.0
        )
        tripped_now = (
            not self.state.halted
            and drawdown_pct + 1e-9 >= self.settings.max_daily_drawdown_pct
        )
        if tripped_now:
            self.state.halted = True
            self.state.halt_reason = (
                f"drawdown de {drawdown_pct:.2f}% nas ultimas 24h "
                f"(limite {self.settings.max_daily_drawdown_pct:.2f}%)"
            )
            self.state.halted_at = now
            logger.critical(
                "CIRCUIT BREAKER ACIONADO: %s. Novas entradas bloqueadas; "
                "encerrando operacoes.",
                self.state.halt_reason,
            )
        self.state.last_cycle_at = now
        self._save()
        return CircuitBreakerReport(
            tripped_now=tripped_now,
            halted=self.state.halted,
            drawdown_pct=drawdown_pct,
            peak_equity=peak,
            limit_pct=self.settings.max_daily_drawdown_pct,
        )

    def size_trade(
        self,
        symbol: str,
        equity: float,
        free_quote: float,
        stop_distance_pct: float,
        tp_rr: float,
        reference_price: float,
        min_notional: float,
        now: float | None = None,
    ) -> RiskDecision:
        now = now or time.time()
        if self._roll_day(now):
            self._save()
        if self.state.halted:
            return RiskDecision(False, reason="circuit breaker ativo")
        if symbol in self.state.positions:
            return RiskDecision(False, reason="ja existe posicao neste par")
        if len(self.state.positions) >= self.settings.max_open_positions:
            return RiskDecision(False, reason="limite de posicoes simultaneas atingido")
        if self.state.trades_today >= self.settings.max_trades_per_day:
            return RiskDecision(False, reason="limite de trades diarios atingido")
        cooldown = self.symbol_cooldown_remaining(symbol, now)
        if cooldown > 0:
            return RiskDecision(
                False,
                reason=f"cooldown apos perda ainda ativo ({cooldown / 60:.0f} min)",
            )
        if equity <= 0 or free_quote <= 0 or reference_price <= 0:
            return RiskDecision(False, reason="saldo ou preco invalido")
        if not 1.5 <= tp_rr <= 10.0:
            return RiskDecision(False, reason="relacao risco-retorno invalida")
        stop_fraction = stop_distance_pct / 100.0
        if not 0.0 < stop_fraction < 0.25:
            return RiskDecision(False, reason="distancia de stop invalida")

        exposure_cap = equity * self.settings.max_total_exposure_pct / 100.0
        exposure_room = exposure_cap - self.open_exposure_quote()
        if exposure_room <= 0:
            return RiskDecision(False, reason="exposicao total maxima atingida")

        reserve = equity * self.settings.min_quote_reserve_pct / 100.0
        spendable = free_quote - reserve
        if spendable <= 0:
            return RiskDecision(
                False,
                reason="saldo livre abaixo da reserva minima de caixa",
            )
        cash_for_notional = spendable / (1.0 + self.settings.fee_rate)

        risk_budget = equity * self.settings.risk_per_trade_pct / 100.0
        risk_fraction = stop_fraction + 2.0 * self.settings.fee_rate
        notional_by_risk = risk_budget / risk_fraction
        notional_by_position = equity * self.settings.max_position_pct / 100.0
        notional = min(
            notional_by_risk,
            notional_by_position,
            cash_for_notional,
            exposure_room,
        )
        notional = math.floor(notional * 100.0) / 100.0
        required_minimum = max(min_notional, self.settings.min_order_quote)
        if notional < required_minimum:
            return RiskDecision(
                False,
                reason=(
                    f"notional {notional:.2f} abaixo do minimo "
                    f"{required_minimum:.2f}"
                ),
            )

        stop_price = reference_price * (1.0 - stop_fraction)
        take_profit_price = reference_price * (
            1.0 + stop_fraction * tp_rr
        )
        risk_quote = notional * risk_fraction
        return RiskDecision(
            approved=True,
            quote_amount=notional,
            stop_distance_pct=stop_distance_pct,
            take_profit_rr=tp_rr,
            risk_quote=risk_quote,
            reference_price=reference_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            reason="aprovado",
        )

    def register_entry(self, position: Position) -> None:
        if position.symbol in self.state.positions:
            raise StateStoreError(
                f"Posicao duplicada para {position.symbol} no estado."
            )
        self._roll_day(time.time())
        self.state.positions[position.symbol] = position
        self.state.trades_today += 1
        self._save()
        logger.info(
            "Posicao registrada: %s amount=%.8f entry=%.8f stop=%.8f tp=%.8f",
            position.symbol,
            position.amount,
            position.entry_price,
            position.stop_price,
            position.take_profit_price,
        )

    def record_close(
        self,
        symbol: str,
        exit_price: float,
        reason: str,
        exit_fee_quote: float = 0.0,
        exit_amount: float | None = None,
        now: float | None = None,
    ) -> TradeClose:
        position = self.state.positions.get(symbol)
        if position is None:
            raise KeyError(f"Nao existe posicao registrada para {symbol}.")
        if not math.isfinite(exit_price) or exit_price <= 0:
            raise ValueError("Preco de saida invalido.")
        now = now or time.time()
        self._roll_day(now)
        amount = exit_amount if exit_amount is not None else position.amount
        amount = min(max(amount, 0.0), position.amount)
        if amount <= 0:
            raise ValueError("Quantidade de saida invalida.")
        fee_share = position.entry_fee_quote * (amount / position.amount)
        pnl = (
            (exit_price - position.entry_price) * amount
            - fee_share
            - exit_fee_quote
        )
        basis = position.entry_price * amount
        pnl_pct = pnl / basis * 100.0 if basis > 0 else 0.0
        trade = {
            "symbol": symbol,
            "amount": amount,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "pnl_quote": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "opened_at": position.opened_at,
            "closed_at": now,
        }
        self.state.trades.append(trade)
        if len(self.state.trades) > TRADE_HISTORY_LIMIT:
            self.state.trades = self.state.trades[-TRADE_HISTORY_LIMIT:]
        del self.state.positions[symbol]
        if (
            pnl < 0
            and self.settings.cooldown_after_loss_minutes > 0
        ):
            self.state.cooldowns[symbol] = (
                now + self.settings.cooldown_after_loss_minutes * 60
            )
        self._save()
        return TradeClose(
            symbol=symbol,
            amount=amount,
            exit_price=exit_price,
            pnl_quote=pnl,
            pnl_pct=pnl_pct,
            reason=reason,
            closed_at=now,
        )

    def proposed_trailing_stop(
        self, position: Position, last_price: float, atr_value: float
    ) -> float | None:
        if not self.settings.enable_trailing_stop:
            return None
        if last_price <= 0 or atr_value <= 0:
            return None
        initial_risk = position.entry_price - position.initial_stop_price
        if initial_risk <= 0:
            return None
        r_multiple = (last_price - position.entry_price) / initial_risk
        if r_multiple < self.settings.breakeven_after_r:
            return None
        candidate = max(
            position.stop_price,
            position.entry_price * (1.0 + 2.0 * self.settings.fee_rate),
        )
        trailing_candidate = (
            position.highest_price - self.settings.trailing_atr_mult * atr_value
        )
        candidate = max(candidate, trailing_candidate)
        candidate = min(candidate, last_price * 0.998)
        if candidate <= position.stop_price:
            return None
        improvement_pct = (
            (candidate - position.stop_price) / position.stop_price * 100.0
        )
        if improvement_pct < self.settings.min_stop_move_pct:
            return None
        return candidate

    def save(self) -> None:
        self._save()

    def reset_halt(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.halted_at = None
        self.state.equity_history = []
        self.state.cooldowns = {}
        self._save()
        logger.warning(
            "Circuit breaker resetado manualmente. Posicoes abertas: %s.",
            ", ".join(self.state.positions) or "nenhuma",
        )

    def _roll_day(self, now: float) -> bool:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.state.trade_day == day:
            return False
        self.state.trade_day = day
        self.state.trades_today = 0
        return True

    def _load_state(self) -> BotState:
        path = self.state_path
        backup = path.with_name(path.name + ".bak")
        if not path.exists() and not backup.exists():
            return BotState()
        if not path.exists():
            logger.critical(
                "Estado principal ausente; restaurando o backup %s.", backup.name
            )
            try:
                data = self._read_json(backup)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise StateStoreError(
                    f"Backup de estado ilegivel em {backup}."
                ) from exc
            state = self._validate_state(data, backup)
            try:
                self._write_json_atomic(data)
            except OSError as exc:
                raise StateStoreError(
                    f"Falha ao restaurar estado a partir do backup: {exc}"
                ) from exc
            self._secure_state_files(path, backup)
            return state
        try:
            data = self._read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.critical(
                "Estado principal ilegivel (%s). Tentando backup.", type(exc).__name__
            )
            if not backup.exists():
                raise StateStoreError(
                    f"Estado corrompido em {path} e sem backup valido."
                ) from exc
            try:
                data = self._read_json(backup)
            except (OSError, ValueError, json.JSONDecodeError) as backup_exc:
                raise StateStoreError(
                    f"Estado e backup corrompidos em {path}."
                ) from backup_exc
            state = self._validate_state(data, backup)
            corrupt_path = path.with_name(
                f"{path.name}.corrupt-{int(time.time())}"
            )
            try:
                shutil.move(str(path), str(corrupt_path))
            except OSError:
                pass
            self._write_json_atomic(data)
        else:
            state = self._validate_state(data, path)
        self._secure_state_files(path, backup)
        return state

    def _validate_state(self, data: dict[str, Any], path: Path) -> BotState:
        try:
            return self._state_from_dict(data)
        except (TypeError, ValueError, KeyError) as exc:
            raise StateStoreError(f"Estado invalido em {path}: {exc}") from exc

    @staticmethod
    def _secure_state_files(path: Path, backup: Path) -> None:
        if os.name != "posix":
            return
        for target in (path, backup):
            try:
                target.chmod(0o600)
            except OSError:
                pass

    def _read_json(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("JSON de estado nao e um objeto.")
        return data

    def _state_from_dict(self, data: dict[str, Any]) -> BotState:
        version = int(data.get("version", 0))
        if version != STATE_VERSION:
            raise ValueError(
                f"versao de estado {version} incompativel (esperado {STATE_VERSION})"
            )
        positions_raw = data.get("positions") or {}
        if not isinstance(positions_raw, dict):
            raise ValueError("positions invalido")
        positions: dict[str, Position] = {}
        for symbol, raw in positions_raw.items():
            if not isinstance(raw, dict):
                raise ValueError(f"posicao invalida para {symbol}")
            payload = dict(raw)
            payload.setdefault("symbol", symbol)
            position = Position.from_dict(payload)
            if position.symbol != symbol:
                raise ValueError(
                    f"simbolo {position.symbol} nao corresponde a chave {symbol}"
                )
            positions[position.symbol] = position

        equity_history: list[dict[str, float]] = []
        for entry in data.get("equity_history") or []:
            if not isinstance(entry, dict):
                continue
            ts = _number(entry.get("ts"))
            equity = _number(entry.get("equity"))
            if ts > 0 and equity > 0:
                equity_history.append({"ts": ts, "equity": equity})
        equity_history.sort(key=lambda item: item["ts"])
        equity_history = equity_history[-EQUITY_HISTORY_LIMIT:]

        trades = data.get("trades") or []
        if not isinstance(trades, list):
            trades = []
        trades = [trade for trade in trades if isinstance(trade, dict)]
        trades = trades[-TRADE_HISTORY_LIMIT:]

        cooldowns_raw = data.get("cooldowns") or {}
        cooldowns: dict[str, float] = {}
        if isinstance(cooldowns_raw, dict):
            for symbol, until in cooldowns_raw.items():
                value = _number(until)
                if value > 0:
                    cooldowns[str(symbol)] = value

        return BotState(
            version=STATE_VERSION,
            halted=bool(data.get("halted", False)),
            halt_reason=str(data.get("halt_reason") or ""),
            halted_at=_optional_timestamp(data.get("halted_at")),
            positions=positions,
            equity_history=equity_history,
            trades=trades,
            trade_day=str(data.get("trade_day") or ""),
            trades_today=max(int(_number(data.get("trades_today"))), 0),
            cooldowns=cooldowns,
            last_cycle_at=_optional_timestamp(data.get("last_cycle_at")),
        )

    def _state_to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "halted_at": self.state.halted_at,
            "positions": {
                symbol: position.to_dict()
                for symbol, position in self.state.positions.items()
            },
            "equity_history": self.state.equity_history,
            "trades": self.state.trades,
            "trade_day": self.state.trade_day,
            "trades_today": self.state.trades_today,
            "cooldowns": self.state.cooldowns,
            "last_cycle_at": self.state.last_cycle_at,
        }

    def _save(self) -> None:
        try:
            self._write_json_atomic(self._state_to_dict())
        except OSError as exc:
            raise StateStoreError(
                f"Falha ao salvar estado em {self.state_path}: {exc}"
            ) from exc
        backup = self.state_path.with_name(self.state_path.name + ".bak")
        try:
            shutil.copyfile(self.state_path, backup)
        except OSError:
            logger.warning("Nao foi possivel atualizar o backup do estado.")
        if os.name == "posix":
            try:
                self.state_path.chmod(0o600)
                backup.chmod(0o600)
            except OSError:
                pass

    def _write_json_atomic(self, data: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)
