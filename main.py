from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import ccxt

from analyzer import (
    StatisticsEngine,
    analyze_recent_market,
    print_market_analysis,
    print_statistics_report,
)
from config import Settings
from exchange import (
    ExchangeClient,
    ExchangeConfigurationError,
    InsufficientBalanceError,
    OrderResult,
    OrderValidationError,
    SecurityViolationError,
)
from logger import AlertService, configure_logging
from risk_manager import (
    CircuitBreakerReport,
    Position,
    RiskDecision,
    RiskManager,
    StateStoreError,
    TradeClose,
)
from strategy import Signal, Strategy
from web_server import DashboardServer

VERSION = "1.0.0"
logger = logging.getLogger("bot.main")

# Inicializar motor de estatísticas
statistics = StatisticsEngine(settings=Settings(), history_size=500)

# Contador para relatórios periodicos
report_counter = 0
REPORT_INTERVAL = 10  # A cada 10 ciclos (aprox. a cada 50 minutos)


class BotHalted(RuntimeError):
    pass


class ManualInterventionRequired(RuntimeError):
    pass


class FatalExecutionError(RuntimeError):
    pass


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _ticker_price(ticker: Mapping[str, Any] | None) -> float:
    if not ticker:
        return 0.0
    for key in ("last", "bid", "ask"):
        value = _as_float(ticker.get(key))
        if value > 0:
            return value
    return 0.0


class TradingBot:
    def __init__(self, settings: Settings, web_enabled: bool = False) -> None:
        self.settings = settings
        self.web_enabled = web_enabled
        self.web_server: DashboardServer | None = None
        self.risk = RiskManager(settings)
        extra_symbols = tuple(
            symbol
            for symbol in self.risk.positions
            if symbol not in settings.symbols
        )
        exchange_settings = settings
        if extra_symbols:
            exchange_settings = replace(
                settings, symbols=(*settings.symbols, *extra_symbols)
            )
            logger.warning(
                "Posicoes fora da lista SYMBOLS: %s. "
                "Os pares foram mantidos para reconciliacao e saida.",
                ", ".join(extra_symbols),
            )
        self.exchange = ExchangeClient(exchange_settings)
        self.strategy = Strategy(settings)
        self.alerter = AlertService(settings)
        self._stop_event = asyncio.Event()
        self._candles_cache: dict[str, list[list[float]]] = {}
        self._closed_this_cycle: set[str] = set()
        self._shutdown_signals_installed = False

    async def run(self) -> int:
        self.alerter.start()
        self.alerter.install_log_handler()
        self._log_startup()
        try:
            await self.exchange.initialize()
            if not self.settings.dry_run:
                await self._reconcile_startup()
            if self.risk.halted:
                logger.critical(
                    "Bot travado pelo circuit breaker (%s). Intervencao manual "
                    "necessaria: python main.py --reset-halt",
                    self.risk.state.halt_reason,
                )
                return 78
            if self.settings.dry_run:
                logger.warning(
                    "MODO DRY-RUN: sinais e dimensionamento serao calculados, "
                    "mas NENHUMA ordem sera enviada."
                )
            else:
                logger.warning(
                    "MODO %s: ordens reais serao enviadas para %s.",
                    "TESTNET/SANDBOX" if self.settings.sandbox else "LIVE",
                    self.settings.exchange_id,
                )
            self._install_signal_handlers()
            if self.web_enabled:
                self.web_server = DashboardServer(
                    self.settings,
                    statistics,
                    self.risk,
                    exchange=self.exchange,
                )
                await self.web_server.start()
                logger.info("Dashboard web em http://localhost:%s", self.web_server.port)
            self.alerter.notify(
                f"{self.settings.bot_name} iniciado | modo={self._mode_label()} | "
                f"pares={', '.join(self.settings.symbols)} | "
                f"intervalo={self.settings.scan_interval_seconds}s"
            )
            logger.info(
                "Loop iniciado. Varredura a cada %ss. Pressione Ctrl+C para parar.",
                self.settings.scan_interval_seconds,
            )
            while not self._stop_event.is_set():
                try:
                    await self._scan_cycle()
                except (BotHalted, FatalExecutionError, StateStoreError):
                    raise
                except ccxt.AuthenticationError as exc:
                    logger.critical(
                        "Falha de autenticacao na exchange: %s. Verifique as chaves.",
                        type(exc).__name__,
                    )
                    return 78
                except ccxt.PermissionDenied as exc:
                    logger.critical(
                        "Permissao negada pela exchange: %s. Verifique a API key.",
                        type(exc).__name__,
                    )
                    return 78
                except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
                    logger.error(
                        "Ciclo abortado por falha de conexao (%s). "
                        "Nova tentativa no proximo intervalo.",
                        type(exc).__name__,
                    )
                except Exception:
                    logger.exception(
                        "Erro inesperado no ciclo. O bot continuara no proximo intervalo."
                    )
                await self._sleep_until_next_cycle()
            logger.info("Encerramento solicitado. Ordens de stop permanecem na exchange.")
            return 0
        except (BotHalted, ManualInterventionRequired) as exc:
            logger.critical("Execucao encerrada: %s", exc)
            return 78
        except FatalExecutionError as exc:
            logger.critical(
                "Falha critica de execucao: %s. Reiniciando via supervisor para reconciliar.",
                exc,
            )
            return 1
        except SecurityViolationError as exc:
            logger.critical("VIOLACAO DE SEGURANCA: %s", exc)
            return 78
        except ExchangeConfigurationError as exc:
            logger.critical("Erro de configuracao da exchange: %s", exc)
            return 78
        except ccxt.AuthenticationError:
            logger.critical("Chaves de API invalidas. Corrija o .env e reinicie.")
            return 78
        except StateStoreError as exc:
            logger.critical("Falha de persistencia do estado: %s", exc)
            return 78
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
            logger.error("Rede indisponivel na inicializacao: %s", type(exc).__name__)
            return 1
        finally:
            if self.web_server is not None:
                await self.web_server.stop()
            await self.alerter.stop()
            await self.exchange.close()
            logger.info("Bot finalizado.")

    def _log_startup(self) -> None:
        mode = self._mode_label()
        logger.info(
            "%s v%s iniciando | modo=%s | exchange=%s | pares=%s | timeframe=%s",
            self.settings.bot_name,
            VERSION,
            mode,
            self.settings.exchange_id,
            ", ".join(self.settings.symbols),
            self.settings.timeframe,
        )
        logger.info(
            "Risco: max_pos=%.2f%% | risco/trade=%.2f%% | drawdown_max=%.2f%% | "
            "exposicao_max=%.2f%% | reserva=%.2f%% | RR=1:%.2f",
            self.settings.max_position_pct,
            self.settings.risk_per_trade_pct,
            self.settings.max_daily_drawdown_pct,
            self.settings.max_total_exposure_pct,
            self.settings.min_quote_reserve_pct,
            self.settings.tp_rr,
        )
        logger.info(
            "Arquivos: estado=%s | logs=%s | telegram=%s",
            self.risk.state_path,
            self.settings.log_dir,
            "on" if self.alerter.enabled else "off",
        )

    def _mode_label(self) -> str:
        if self.settings.dry_run:
            return "DRY-RUN"
        if self.settings.sandbox:
            return "TESTNET"
        return "LIVE"

    def _install_signal_handlers(self) -> None:
        if self._shutdown_signals_installed:
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown, sig)
            except (NotImplementedError, RuntimeError):
                pass
        self._shutdown_signals_installed = True

    def _request_shutdown(self, sig: signal.Signals) -> None:
        logger.warning("Sinal %s recebido; finalizando com seguranca.", sig.name)
        self._stop_event.set()

    async def _sleep_until_next_cycle(self) -> None:
        interval = self.settings.scan_interval_seconds * random.uniform(0.9, 1.1)
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    def _record_decision(self, action: str, **fields: object) -> None:
        if self.web_server is None:
            return
        self.web_server.record_decision({"action": action, **fields})

    async def _scan_cycle(self) -> None:
        now = datetime.now(timezone.utc)
        global report_counter
        report_counter += 1
        self._candles_cache.clear()
        self._closed_this_cycle.clear()
        symbols = sorted(set(self.settings.symbols) | set(self.risk.positions))
        tickers = await self.exchange.fetch_tickers(symbols)
        if not tickers:
            raise ccxt.NetworkError("Nenhum ticker disponível.")
        for symbol in self.risk.positions:
            if symbol not in tickers:
                tickers[symbol] = await self.exchange.fetch_ticker(symbol)

        # Registrar preços atuais no motor de estatísticas
        for ticker in tickers.values():
            price = _ticker_price(ticker)
            if price > 0:
                statistics.add_price(price, now)

        if self.settings.dry_run:
            equity = self.settings.paper_equity
            free_quote = self.settings.paper_equity
            logger.info(
                "Ciclo DRY-RUN | equity simulada=%.2f %s | sinais serao apenas registrados",
                equity,
                self.settings.quote_currency,
            )
        else:
            balance = await self.exchange.fetch_balance()
            equity = self._compute_equity(balance, tickers)
            free_quote = self.exchange.balance_free(
                balance, self.settings.quote_currency
            )
            report = self.risk.record_equity(equity, now.timestamp())
            logger.info(
                "Ciclo | equity=%.2f %s | livre=%.2f | drawdown_24h=%.2f%% | "
                "posicoes=%s | exposicao=%.2f",
                equity,
                self.settings.quote_currency,
                free_quote,
                report.drawdown_pct,
                ", ".join(sorted(self.risk.positions)) or "nenhuma",
                self.risk.open_exposure_quote(),
            )
            if report.tripped_now:
                await self._handle_circuit_breaker(report, tickers, now)
                raise BotHalted("circuit breaker diario acionado")
            if report.halted:
                raise BotHalted("bot esta travado pelo circuit breaker")

        if not self.settings.dry_run:
            await self._manage_positions(tickers, now)

        if self.risk.state.trades_today >= self.settings.max_trades_per_day:
            logger.info(
                "Limite de %s trades no dia atingido; somente gestao de posicoes.",
                self.settings.max_trades_per_day,
            )
            return
        slots = self.settings.max_open_positions - len(self.risk.positions)
        if slots <= 0:
            logger.info("Limite de posicoes simultaneas atingido.")
            return

        candidates: list[Signal] = []
        for symbol in self.settings.symbols:
            if self._stop_event.is_set():
                return
            if symbol in self.risk.positions or symbol in self._closed_this_cycle:
                continue
            cooldown = self.risk.symbol_cooldown_remaining(
                symbol, now.timestamp()
            )
            if cooldown > 0:
                logger.info(
                    "%s em cooldown por perda (%.0f min restantes).",
                    symbol,
                    cooldown / 60,
                )
                continue
            ticker = tickers.get(symbol)
            if ticker is None:
                ticker = await self.exchange.fetch_ticker(symbol)
                tickers[symbol] = ticker
            try:
                candles = await self._get_candles(symbol)
                signal = self.strategy.evaluate(symbol, candles, ticker, now)
            except Exception as exc:
                logger.warning(
                    "Falha ao analisar %s (%s); seguindo para o proximo par.",
                    symbol,
                    type(exc).__name__,
                )
                continue
            if signal.is_buy:
                logger.info(
                    "Sinal de compra em %s: %s",
                    symbol,
                    " | ".join(signal.reasons),
                )
                statistics.update_signal(
                    {
                        "action": signal.action.value,
                        "score": signal.score,
                        "max_score": signal.max_score,
                        "stop_distance_pct": signal.stop_distance_pct,
                        "atr_pct": signal.atr_pct,
                        "reasons": signal.reasons,
                    },
                    signal.price,
                    now,
                )
                candidates.append(signal)
                self._record_decision(
                    "signal",
                    symbol=signal.symbol,
                    action=signal.action.value,
                    score=signal.score,
                    max_score=signal.max_score,
                    stop_distance_pct=signal.stop_distance_pct,
                    atr_pct=signal.atr_pct,
                    reasons=signal.reasons,
                    price=signal.price,
                )
            else:
                logger.info(
                    "Sem entrada em %s: %s", symbol, signal.reasons[0]
                )

        candidates.sort(
            key=lambda item: (item.score, -item.stop_distance_pct), reverse=True
        )
        for signal in candidates:
            if self._stop_event.is_set():
                return
            if len(self.risk.positions) >= self.settings.max_open_positions:
                break
            if signal.symbol in self.risk.positions:
                continue
            limits = self.exchange.market_limits(signal.symbol)
            decision = self.risk.size_trade(
                symbol=signal.symbol,
                equity=equity,
                free_quote=free_quote,
                stop_distance_pct=signal.stop_distance_pct,
                tp_rr=signal.tp_rr,
                reference_price=signal.price,
                min_notional=limits.min_cost,
                now=now.timestamp(),
            )
            if not decision.approved:
                logger.info(
                    "Entrada em %s bloqueada pelo risco: %s",
                    signal.symbol,
                    decision.reason,
                )
                continue
            logger.info(
                "Risco aprovado %s | notional=%.2f %s | risco_estimado=%.2f | "
                "stop=%.8f | TP=%.8f | score=%s/%s",
                signal.symbol,
                decision.quote_amount,
                self.settings.quote_currency,
                decision.risk_quote,
                decision.stop_price,
                decision.take_profit_price,
                signal.score,
                signal.max_score,
            )
            if self.settings.dry_run:
                logger.info(
                    "DRY-RUN: compraria %.2f %s em %s. Nenhuma ordem enviada.",
                    decision.quote_amount,
                    self.settings.quote_currency,
                    signal.symbol,
                )
                continue
            try:
                position = await self._execute_entry(signal, decision, now)
            except FatalExecutionError:
                raise
            except ccxt.AuthenticationError:
                raise
            except Exception as exc:
                logger.error(
                    "Falha ao executar entrada em %s: %s",
                    signal.symbol,
                    type(exc).__name__,
                )
                continue
            if position is not None:
                free_quote = max(
                    0.0,
                    free_quote
                    - decision.quote_amount
                    - position.entry_fee_quote,
                )
                equity = max(0.0, equity - position.entry_fee_quote)

        # Relatório periódico de estatísticas e mercado
        if report_counter >= REPORT_INTERVAL:
            report_counter = 0
            stats_summary = statistics.get_summary()
            print_statistics_report(stats_summary)
            for symbol in self.settings.symbols:
                ticker = tickers.get(symbol)
                if ticker is None:
                    continue
                market = await analyze_recent_market(
                    self.exchange,
                    symbol,
                    self.settings.timeframe,
                    self.settings,
                )
                print_market_analysis(market)
                break

    def _compute_equity(
        self, balance: Mapping[str, Any], tickers: Mapping[str, Mapping[str, Any]]
    ) -> float:
        equity = self.exchange.balance_total(
            balance, self.settings.quote_currency
        )
        for symbol, position in self.risk.positions.items():
            ticker = tickers.get(symbol)
            price = _ticker_price(ticker)
            if price <= 0:
                price = position.entry_price
                logger.warning(
                    "Sem preco atual para %s; usando preco de entrada no equity.",
                    symbol,
                )
            equity += position.amount * price
        return equity

    async def _get_candles(self, symbol: str) -> list[list[float]]:
        cached = self._candles_cache.get(symbol)
        if cached is not None:
            return cached
        candles = await self.exchange.fetch_ohlcv(
            symbol,
            self.settings.timeframe,
            self.settings.ohlcv_limit + 1,
        )
        self._candles_cache[symbol] = candles
        return candles

    def _dust(self, symbol: str) -> float:
        limits = self.exchange.market_limits(symbol)
        return max(limits.min_amount * 0.5, 1e-12)

    async def _wait_for_free_base(
        self,
        symbol: str,
        expected: float,
        attempts: int = 5,
        delay: float = 0.6,
    ) -> float:
        base = self.exchange.base_currency(symbol)
        target = max(expected * 0.99, 0.0)
        last_free = 0.0
        for attempt in range(attempts):
            balance = await self.exchange.fetch_balance()
            last_free = self.exchange.balance_free(balance, base)
            if last_free >= target:
                return last_free
            if last_free <= self._dust(symbol) and attempt >= 1:
                return last_free
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
        return last_free

    async def _find_bot_stop(self, symbol: str) -> OrderResult | None:
        try:
            open_orders = await self.exchange.fetch_open_orders(symbol)
        except Exception as exc:
            logger.error(
                "Falha ao listar ordens abertas de %s: %s",
                symbol,
                type(exc).__name__,
            )
            return None
        for order in open_orders:
            order_type = order.type.lower()
            has_trigger = any(
                key in order.raw for key in ("stopPrice", "triggerPrice")
            )
            if "stop" in order_type or has_trigger:
                return order
        return None

    def _stop_order_is_protective(
        self, order: OrderResult, amount: float
    ) -> bool:
        if not order.is_open or order.filled > 0 or order.amount <= 0:
            return False
        try:
            step = self.exchange.market_limits(order.symbol).amount_step
        except ExchangeConfigurationError:
            step = 0.0
        tolerance = max(step, amount * 0.001)
        return order.amount + tolerance >= amount

    async def _position_has_protective_stop(self, position: Position) -> bool:
        if position.stop_order_id:
            try:
                order = await self.exchange.fetch_order(
                    position.symbol, order_id=position.stop_order_id
                )
            except Exception as exc:
                logger.error(
                    "Falha ao verificar stop de %s: %s",
                    position.symbol,
                    type(exc).__name__,
                )
                return False
            if order is not None and self._stop_order_is_protective(
                order, position.amount
            ):
                return True
        adopted = await self._find_bot_stop(position.symbol)
        if adopted is not None and self._stop_order_is_protective(
            adopted, position.amount
        ):
            position.stop_order_id = adopted.id
            self.risk.save()
            return True
        return False

    async def _manage_positions(
        self,
        tickers: Mapping[str, Mapping[str, Any]],
        now: datetime,
    ) -> None:
        for symbol, position in list(self.risk.positions.items()):
            if self._stop_event.is_set():
                logger.info("Encerramento pendente; gestao de posicoes interrompida.")
                return
            ticker = tickers.get(symbol)
            if ticker is None:
                try:
                    ticker = await self.exchange.fetch_ticker(symbol)
                except Exception as exc:
                    logger.error(
                        "Sem ticker para posicao %s: %s", symbol, type(exc).__name__
                    )
                    continue
            last_price = _ticker_price(ticker)
            if last_price <= 0:
                logger.warning("Preco invalido para %s; pulando gestao.", symbol)
                continue
            try:
                await self._manage_single_position(position, last_price, now)
            except FatalExecutionError:
                raise
            except Exception as exc:
                logger.error(
                    "Erro ao gerenciar %s: %s", symbol, type(exc).__name__
                )

    async def _manage_single_position(
        self, position: Position, last_price: float, now: datetime
    ) -> None:
        symbol = position.symbol
        stop_order: OrderResult | None = None
        if position.stop_order_id:
            stop_order = await self.exchange.fetch_order(
                symbol, order_id=position.stop_order_id
            )
        if stop_order is not None and stop_order.filled > 0:
            logger.critical(
                "Stop executado ou parcial em %s (%.8f preenchido); "
                "liquidando o restante.",
                symbol,
                stop_order.filled,
            )
            await self._handle_stop_exit(position, stop_order, last_price, now)
            return
        if (
            stop_order is None
            or stop_order.is_failed
            or (stop_order.is_closed and stop_order.filled <= 0)
        ):
            adopted = await self._find_bot_stop(symbol)
            if adopted is not None:
                position.stop_order_id = adopted.id
                self.risk.save()
                logger.error(
                    "Ordem de stop de %s recuperada da exchange (id=%s).",
                    symbol,
                    adopted.id,
                )
                stop_order = adopted
                if adopted.filled > 0:
                    await self._handle_stop_exit(
                        position, adopted, last_price, now
                    )
                    return
            else:
                await self._restore_protection(
                    position, last_price, now, "ordem de stop ausente"
                )
                return
        if not self._stop_order_is_protective(stop_order, position.amount):
            logger.critical(
                "Stop de %s nao cobre toda a posicao (ordem=%.8f, posicao=%.8f).",
                symbol,
                stop_order.amount,
                position.amount,
            )
            await self._restore_protection(
                position, last_price, now, "cobertura incompleta do stop"
            )
            return

        if last_price <= position.stop_price * 0.995:
            logger.critical(
                "Preco de %s abaixo do stop sem execucao confirmada; "
                "executando saida de emergencia.",
                symbol,
            )
            await self._force_exit(position, "stop_watchdog", now, last_price)
            return

        if last_price >= position.take_profit_price:
            logger.info(
                "Take-profit atingido em %s (%.8f >= %.8f).",
                symbol,
                last_price,
                position.take_profit_price,
            )
            await self._close_at_market(
                position, "take_profit", now, last_price
            )
            return

        if position.stop_order_id is None:
            return
        changed = last_price > position.highest_price
        if changed:
            position.highest_price = last_price
        candles = await self._get_candles(symbol)
        atr_value = self.strategy.latest_atr(candles, now)
        if atr_value is None:
            if changed:
                self.risk.save()
            return
        new_stop = self.risk.proposed_trailing_stop(
            position, last_price, atr_value
        )
        if new_stop is None:
            if changed:
                self.risk.save()
            return
        await self._replace_stop(position, new_stop, last_price, now)

    async def _execute_entry(
        self, signal: Signal, decision: RiskDecision, now: datetime
    ) -> Position | None:
        symbol = signal.symbol
        buy = await self.exchange.create_market_buy(
            symbol, decision.quote_amount
        )
        fee_base = min(max(buy.fee_base, 0.0), buy.filled)
        amount = self.exchange.floor_amount(
            symbol, buy.filled - fee_base
        )
        entry_price = buy.average or buy.price or signal.price
        if amount <= 0 or entry_price <= 0:
            logger.critical(
                "Compra de %s sem quantidade/preco confirmados. Intervencao manual.",
                symbol,
            )
            raise FatalExecutionError("execucao de compra sem confirmacao")
        if fee_base > 0:
            logger.info(
                "Compra de %s: taxa de %.8f %s descontada da quantidade.",
                symbol,
                fee_base,
                self.exchange.base_currency(symbol),
            )
        stop_price = self.exchange.round_price(
            symbol, entry_price * (1.0 - decision.stop_distance_pct / 100.0)
        )
        take_profit_price = self.exchange.round_price(
            symbol,
            entry_price
            * (
                1.0
                + decision.stop_distance_pct
                / 100.0
                * decision.take_profit_rr
            ),
        )
        if not 0 < stop_price < entry_price < take_profit_price:
            logger.critical(
                "Niveis de risco invalidos para %s. Executando saida de emergencia.",
                symbol,
            )
            raise FatalExecutionError("niveis de stop/take-profit invalidos")
        position = Position(
            symbol=symbol,
            amount=amount,
            entry_price=entry_price,
            entry_notional=buy.cost if buy.cost > 0 else decision.quote_amount,
            stop_price=stop_price,
            initial_stop_price=stop_price,
            take_profit_price=take_profit_price,
            highest_price=entry_price,
            entry_order_id=buy.id,
            stop_order_id=None,
            entry_fee_quote=buy.fee_quote,
            opened_at=now.timestamp(),
        )
        try:
            self.risk.register_entry(position)
        except Exception as exc:
            logger.critical(
                "Compra de %s executada, mas o estado nao pode ser persistido. "
                "Tentando proteger a posicao e encerrando.",
                symbol,
            )
            try:
                stop_order = await self.exchange.place_stop_loss(
                    symbol, position.amount, stop_price
                )
                logger.critical(
                    "Stop de emergencia colocado em %s (id=%s). "
                    "Intervencao manual obrigatoria.",
                    symbol,
                    stop_order.id,
                )
            except Exception as stop_exc:
                logger.critical(
                    "Falha ao colocar stop de emergencia em %s: %s",
                    symbol,
                    type(stop_exc).__name__,
                )
            raise FatalExecutionError(
                "falha ao persistir nova posicao"
            ) from exc

        try:
            stop_order = await self.exchange.place_stop_loss(
                symbol, position.amount, stop_price
            )
            if stop_order.filled <= 0 and not self._stop_order_is_protective(
                stop_order, position.amount
            ):
                raise OrderValidationError(
                    f"stop de {symbol} nao ficou ativo apos o envio"
                )
        except Exception as exc:
            logger.critical(
                "Falha ao colocar stop-loss em %s: %s. Tentando sair a mercado.",
                symbol,
                type(exc).__name__,
            )
            try:
                closed = await self._close_at_market(
                    position, "entry_protection_failed", now, entry_price
                )
            except Exception as sell_exc:
                logger.critical(
                    "Falha critica: posicao %s ficou sem stop e sem venda (%s).",
                    symbol,
                    type(sell_exc).__name__,
                )
                raise FatalExecutionError(
                    "entrada sem protecao e sem saida"
                ) from sell_exc
            if closed:
                return None
            if await self._position_has_protective_stop(position):
                return position
            try:
                await self._restore_protection(
                    position,
                    entry_price,
                    now,
                    "entrada ficou sem stop",
                )
            except Exception as restore_exc:
                raise FatalExecutionError(
                    f"entrada em {symbol} sem stop e sem saida"
                ) from restore_exc
            if symbol not in self.risk.positions:
                return None
            if await self._position_has_protective_stop(position):
                return position
            raise FatalExecutionError(
                f"entrada em {symbol} permanece sem protecao"
            ) from exc

        if stop_order.filled > 0:
            await self._handle_stop_exit(
                position, stop_order, entry_price, now
            )
            return None
        position.stop_order_id = stop_order.id
        self.risk.save()
        message = (
            f"ENTRADA {symbol} | qty={amount:.8f} | preco~{entry_price:.8f} | "
            f"stop={stop_price:.8f} | TP={take_profit_price:.8f} | "
            f"notional={position.entry_notional:.2f} {self.settings.quote_currency}"
        )
        logger.info(message)
        self.alerter.notify(message)
        self._record_decision(
            "entry",
            symbol=symbol,
            entry_price=entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            notional=position.entry_notional,
            amount=position.amount,
        )
        return position

    async def _restore_stop_only(
        self,
        position: Position,
        last_price: float,
        now: datetime,
    ) -> None:
        symbol = position.symbol
        if position.stop_order_id is not None:
            existing = await self.exchange.fetch_order(
                symbol, order_id=position.stop_order_id
            )
            if existing is not None and existing.filled > 0:
                await self._handle_stop_exit(position, existing, last_price, now)
                return
            if existing is not None and existing.is_open:
                if self._stop_order_is_protective(existing, position.amount):
                    return
                logger.critical(
                    "Stop existente de %s nao cobre a posicao; "
                    "cancelando para recolocar.",
                    symbol,
                )
                if not await self.exchange.cancel_order(symbol, existing.id):
                    raise OrderValidationError(
                        f"stop incompleto de {symbol} nao pode ser cancelado"
                    )
                position.stop_order_id = None
                self.risk.save()
        balance = await self.exchange.fetch_balance()
        base = self.exchange.base_currency(symbol)
        total = self.exchange.balance_total(balance, base)
        dust = self._dust(symbol)
        if total <= dust:
            close = self.risk.record_close(
                symbol,
                last_price,
                "posicao_ausente_na_exchange",
                exit_amount=position.amount,
                now=now.timestamp(),
            )
            self._log_trade_close(close)
            return
        allowed = min(position.amount, total)
        if allowed < position.amount * 0.999:
            ratio = allowed / position.amount
            position.amount = allowed
            position.entry_notional *= ratio
            position.entry_fee_quote *= ratio
            self.risk.save()
            logger.critical(
                "Saldo de %s menor que o registrado; posicao ajustada para %.8f.",
                symbol,
                allowed,
            )
        stop_price = self.exchange.round_price(symbol, position.stop_price)
        order = await self.exchange.place_stop_loss(
            symbol, position.amount, stop_price
        )
        if order.filled > 0:
            await self._handle_stop_exit(position, order, last_price, now)
            return
        if not self._stop_order_is_protective(order, position.amount):
            raise OrderValidationError(
                f"stop de {symbol} nao ficou ativo apos o envio"
            )
        position.stop_order_id = order.id
        self.risk.save()
        logger.error(
            "Protecao restaurada para %s em stop=%.8f (ordem=%s).",
            symbol,
            stop_price,
            order.id,
        )

    async def _restore_protection(
        self,
        position: Position,
        last_price: float,
        now: datetime,
        reason: str,
    ) -> None:
        symbol = position.symbol
        logger.error("Protecao ausente em %s (%s).", symbol, reason)
        if last_price <= position.stop_price:
            await self._force_exit(position, "protecao_ausente", now, last_price)
            return
        try:
            await self._restore_stop_only(position, last_price, now)
        except FatalExecutionError:
            raise
        except Exception as exc:
            logger.critical(
                "Falha ao restaurar stop de %s: %s. Tentando liquidar.",
                symbol,
                type(exc).__name__,
            )
            try:
                await self._close_at_market(
                    position, "protection_restore_failed", now, last_price
                )
            except Exception as sell_exc:
                raise FatalExecutionError(
                    f"posicao {symbol} sem protecao apos falha de restauracao"
                ) from sell_exc

    async def _handle_stop_exit(
        self,
        position: Position,
        stop_order: OrderResult,
        last_price: float,
        now: datetime,
    ) -> None:
        symbol = position.symbol
        filled = min(max(stop_order.filled, 0.0), position.amount)
        if filled <= 0:
            await self._restore_protection(
                position, last_price, now, "stop encerrado sem execucao"
            )
            return
        exit_value = (
            stop_order.cost
            if stop_order.cost > 0
            else filled * (stop_order.average or stop_order.price or last_price)
        )
        fee = stop_order.fee_quote
        try:
            await self.exchange.cancel_bot_orders([symbol])
        except Exception as exc:
            logger.error(
                "Falha ao cancelar ordens residuais de %s: %s",
                symbol,
                type(exc).__name__,
            )
        dust = self._dust(symbol)
        remaining = position.amount - filled
        if remaining > dust:
            try:
                free = await self._wait_for_free_base(symbol, remaining)
                if free > dust:
                    extra = await self.exchange.create_market_sell(
                        symbol, min(remaining, free)
                    )
                    extra_filled = min(extra.filled, remaining)
                    extra_value = (
                        extra.cost
                        if extra.cost > 0
                        else extra_filled
                        * (extra.average or extra.price or last_price)
                    )
                    filled += extra_filled
                    exit_value += extra_value
                    fee += extra.fee_quote
                else:
                    balance = await self.exchange.fetch_balance()
                    total = self.exchange.balance_total(
                        balance, self.exchange.base_currency(symbol)
                    )
                    if total <= dust:
                        filled = position.amount
                        exit_value = filled * last_price
            except Exception as exc:
                logger.critical(
                    "Stop executado, mas restante de %s nao pode ser vendido: %s",
                    symbol,
                    type(exc).__name__,
                )
                rest = position.amount - filled
                if rest <= dust:
                    pass
                else:
                    ratio = rest / position.amount
                    position.amount = rest
                    position.entry_notional *= ratio
                    position.entry_fee_quote *= ratio
                    position.stop_order_id = None
                    self.risk.save()
                    if last_price > position.stop_price:
                        await self._restore_stop_only(
                            position, last_price, now
                        )
                    else:
                        raise FatalExecutionError(
                            f"restante de {symbol} sem protecao apos stop"
                        ) from exc
                    return
        if filled <= 0:
            raise FatalExecutionError(
                f"stop de {symbol} executado sem quantidade valida"
            )
        exit_price = exit_value / filled if exit_value > 0 else last_price
        if filled >= position.amount - dust:
            close = self.risk.record_close(
                symbol,
                exit_price,
                "stop_loss",
                exit_fee_quote=fee,
                exit_amount=min(filled, position.amount),
                now=now.timestamp(),
            )
            self._log_trade_close(close)
        else:
            rest = position.amount - filled
            ratio = rest / position.amount
            position.amount = rest
            position.entry_notional *= ratio
            position.entry_fee_quote *= ratio
            position.stop_order_id = None
            self.risk.save()
            logger.critical(
                "Saida parcial por stop em %s; restante %.8f sera reprotegido.",
                symbol,
                rest,
            )
            if last_price > position.stop_price:
                await self._restore_stop_only(position, last_price, now)
            else:
                raise FatalExecutionError(
                    f"restante de {symbol} sem stop apos execucao parcial"
                )

    async def _close_at_market(
        self,
        position: Position,
        reason: str,
        now: datetime,
        last_price: float,
    ) -> bool:
        symbol = position.symbol
        known = {symbol: {position.stop_order_id} if position.stop_order_id else set()}
        try:
            await self.exchange.cancel_bot_orders([symbol], known)
        except Exception as exc:
            logger.error(
                "Nao foi possivel cancelar ordens de %s antes da venda: %s",
                symbol,
                type(exc).__name__,
            )
            try:
                protected = await self._position_has_protective_stop(position)
            except Exception as verify_exc:
                raise FatalExecutionError(
                    f"falha ao cancelar ordens e nao foi possivel verificar "
                    f"a protecao de {symbol}"
                ) from verify_exc
            if protected:
                return False
            raise FatalExecutionError(
                f"falha ao cancelar ordens e {symbol} esta sem stop"
            ) from exc
        if position.stop_order_id:
            try:
                stop_order = await self.exchange.fetch_order(
                    symbol, order_id=position.stop_order_id
                )
            except Exception as exc:
                raise FatalExecutionError(
                    f"stop de {symbol} cancelado, mas o estado ficou incerto"
                ) from exc
            if stop_order is not None and stop_order.filled > 0 and (
                stop_order.is_closed
                or stop_order.filled >= position.amount * 0.999
            ):
                await self._handle_stop_exit(
                    position, stop_order, last_price, now
                )
                return True
        try:
            free = await self._wait_for_free_base(symbol, position.amount)
            dust = self._dust(symbol)
            if free <= 0:
                balance = await self.exchange.fetch_balance()
                total = self.exchange.balance_total(
                    balance, self.exchange.base_currency(symbol)
                )
                if total <= dust:
                    close = self.risk.record_close(
                        symbol,
                        last_price,
                        f"{reason}_sem_saldo",
                        exit_amount=position.amount,
                        now=now.timestamp(),
                    )
                    self._log_trade_close(close)
                    return True
                raise InsufficientBalanceError(
                    f"saldo de {symbol} bloqueado apos cancelamento"
                )
            to_sell = min(position.amount, free)
            sell = await self.exchange.create_market_sell(symbol, to_sell)
            sold = min(sell.filled, position.amount)
            if sold <= 0:
                raise OrderValidationError(
                    f"venda de {symbol} sem execucao confirmada"
                )
            exit_price = (
                sell.cost / sold
                if sell.cost > 0 and sold > 0
                else (sell.average or sell.price or last_price)
            )
            if sold >= position.amount - dust:
                close = self.risk.record_close(
                    symbol,
                    exit_price,
                    reason,
                    exit_fee_quote=sell.fee_quote,
                    exit_amount=sold,
                    now=now.timestamp(),
                )
                self._log_trade_close(close)
                return True
            rest = position.amount - sold
            ratio = rest / position.amount
            position.amount = rest
            position.entry_notional *= ratio
            position.entry_fee_quote *= ratio
            position.stop_order_id = None
            self.risk.save()
            logger.critical(
                "Venda parcial em %s; restante %.8f sera reprotegido.",
                symbol,
                rest,
            )
            if last_price <= position.stop_price:
                raise FatalExecutionError(
                    f"venda parcial de {symbol} com preco abaixo do stop"
                )
            await self._restore_stop_only(position, last_price, now)
            return False
        except FatalExecutionError:
            raise
        except Exception as exc:
            position_alive = symbol in self.risk.positions
            if position_alive and last_price > position.stop_price:
                try:
                    await self._restore_stop_only(
                        position, last_price, now
                    )
                    logger.critical(
                        "Falha ao vender %s (%s); stop foi recolocado.",
                        symbol,
                        type(exc).__name__,
                    )
                    return False
                except Exception as restore_exc:
                    raise FatalExecutionError(
                        f"posicao {symbol} sem protecao apos falha de venda"
                    ) from restore_exc
            raise FatalExecutionError(
                f"falha de venda em {symbol} sem stop valido"
            ) from exc

    async def _replace_stop(
        self,
        position: Position,
        new_stop: float,
        last_price: float,
        now: datetime,
    ) -> None:
        symbol = position.symbol
        rounded_new = self.exchange.round_price(symbol, new_stop)
        if rounded_new <= position.stop_price or rounded_new >= last_price:
            return
        old_stop = position.stop_price
        if position.stop_order_id:
            old_id = position.stop_order_id
            try:
                await self.exchange.cancel_order(symbol, old_id)
            except Exception as exc:
                logger.error(
                    "Falha ao cancelar stop antigo de %s: %s. "
                    "Verificando se a protecao continua ativa.",
                    symbol,
                    type(exc).__name__,
                )
                try:
                    existing = await self.exchange.fetch_order(
                        symbol, order_id=old_id
                    )
                except Exception as verify_exc:
                    raise FatalExecutionError(
                        f"estado do stop de {symbol} ficou incerto no trailing"
                    ) from verify_exc
                if existing is not None and existing.filled > 0:
                    await self._handle_stop_exit(
                        position, existing, last_price, now
                    )
                    return
                if existing is not None and existing.is_open:
                    logger.warning(
                        "Stop antigo de %s continua ativo; trailing adiado.",
                        symbol,
                    )
                    return
            try:
                actual = await self.exchange.fetch_order(
                    symbol, order_id=old_id
                )
            except Exception as exc:
                logger.error(
                    "Nao foi possivel confirmar o stop antigo de %s: %s. "
                    "Tentando colocar o novo stop.",
                    symbol,
                    type(exc).__name__,
                )
            else:
                if actual is not None and actual.filled > 0:
                    await self._handle_stop_exit(
                        position, actual, last_price, now
                    )
                    return
                if actual is not None and actual.is_open:
                    logger.warning(
                        "Stop antigo de %s ainda aparece aberto; trailing adiado.",
                        symbol,
                    )
                    return
        try:
            free = await self._wait_for_free_base(symbol, position.amount)
            if free <= self._dust(symbol):
                raise InsufficientBalanceError("saldo base indisponivel")
            order = await self.exchange.place_stop_loss(
                symbol, min(position.amount, free), rounded_new
            )
            if order.filled <= 0 and not self._stop_order_is_protective(
                order, position.amount
            ):
                raise OrderValidationError(
                    f"novo stop de {symbol} nao ficou ativo"
                )
        except Exception as exc:
            logger.critical(
                "Falha ao mover stop de %s (%s); tentando restaurar o anterior.",
                symbol,
                type(exc).__name__,
            )
            try:
                free = await self._wait_for_free_base(
                    symbol, position.amount
                )
                restored = await self.exchange.place_stop_loss(
                    symbol, min(position.amount, free), old_stop
                )
                position.stop_order_id = restored.id
                position.stop_price = old_stop
                self.risk.save()
                logger.error(
                    "Stop anterior restaurado em %s (%.8f).", symbol, old_stop
                )
            except Exception as restore_exc:
                raise FatalExecutionError(
                    f"stop de {symbol} perdido durante trailing"
                ) from restore_exc
            return
        if order.filled > 0:
            await self._handle_stop_exit(position, order, last_price, now)
            return
        position.stop_order_id = order.id
        position.stop_price = rounded_new
        self.risk.save()
        logger.info(
            "Stop movido em %s: %.8f -> %.8f (trailing).",
            symbol,
            old_stop,
            rounded_new,
        )

    async def _force_exit(
        self,
        position: Position,
        reason: str,
        now: datetime,
        last_price: float,
    ) -> bool:
        try:
            closed = await self._close_at_market(
                position, reason, now, last_price
            )
        except FatalExecutionError:
            raise
        except Exception as exc:
            logger.critical(
                "Falha ao liquidar %s (%s): %s",
                position.symbol,
                reason,
                type(exc).__name__,
            )
            raise FatalExecutionError(
                f"falha ao liquidar {position.symbol}"
            ) from exc
        if not closed:
            logger.critical(
                "Posicao %s nao foi totalmente liquidada (%s).",
                position.symbol,
                reason,
            )
        return closed

    async def _handle_circuit_breaker(
        self,
        report: CircuitBreakerReport,
        tickers: Mapping[str, Mapping[str, Any]],
        now: datetime,
    ) -> None:
        logger.critical(
            "CIRCUIT BREAKER: drawdown de %.2f%% (limite %.2f%%). "
            "Bloqueando novas entradas.",
            report.drawdown_pct,
            report.limit_pct,
        )
        if not self.settings.circuit_breaker_flatten:
            logger.critical(
                "CIRCUIT_BREAKER_FLATTEN=false: posicoes mantidas com seus stops."
            )
            raise BotHalted("circuit breaker acionado")
        symbols = set(self.settings.symbols) | set(self.risk.positions)
        known = {
            symbol: {
                position.stop_order_id
                for symbol2, position in self.risk.positions.items()
                if symbol2 == symbol and position.stop_order_id
            }
            for symbol in symbols
        }
        try:
            await self.exchange.cancel_bot_orders(symbols, known)
        except Exception as exc:
            logger.critical(
                "Falha ao cancelar ordens no circuit breaker: %s",
                type(exc).__name__,
            )
        failures = 0
        for position in list(self.risk.positions.values()):
            ticker = tickers.get(position.symbol)
            last_price = _ticker_price(ticker) or position.entry_price
            try:
                closed = await self._force_exit(
                    position, "circuit_breaker", now, last_price
                )
                if not closed:
                    failures += 1
            except Exception as exc:
                failures += 1
                logger.critical(
                    "Falha ao liquidar %s no circuit breaker: %s",
                    position.symbol,
                    type(exc).__name__,
                )
        if failures:
            raise FatalExecutionError(
                "circuit breaker acionado, mas nem todas as posicoes foram liquidadas"
            )
        logger.critical(
            "Circuit breaker concluido: posicoes liquidadas e novas entradas bloqueadas."
        )
        raise BotHalted("circuit breaker acionado")

    def _log_trade_close(self, close: TradeClose) -> None:
        self._closed_this_cycle.add(close.symbol)
        message = (
            f"SAIDA {close.symbol} ({close.reason}) | qty={close.amount:.8f} | "
            f"preco={close.exit_price:.8f} | PnL={close.pnl_quote:.2f} "
            f"({close.pnl_pct:+.2f}%)"
        )
        if close.reason == "take_profit":
            logger.info(message)
            self.alerter.notify(message)
        else:
            logger.critical(message)
        self._record_decision(
            "exit",
            symbol=close.symbol,
            reason=close.reason,
            exit_price=close.exit_price,
            pnl_quote=close.pnl_quote,
            pnl_pct=close.pnl_pct,
            amount=close.amount,
        )

    async def _reconcile_startup(self) -> None:
        if not self.risk.positions:
            balance = await self.exchange.fetch_balance()
            untracked = [
                f"{self.exchange.balance_total(balance, self.exchange.base_currency(symbol)):.8f} "
                f"{self.exchange.base_currency(symbol)}"
                for symbol in self.settings.symbols
                if self.exchange.balance_total(
                    balance, self.exchange.base_currency(symbol)
                )
                > self._dust(symbol)
            ]
            if untracked:
                raise ManualInterventionRequired(
                    "saldo em moeda base sem posicao no estado: "
                    + ", ".join(untracked)
                    + ". Restaure o estado ou regularize a conta antes de iniciar."
                )
            logger.info("Reconciliacao: nenhuma posicao aberta no estado.")
            return
        logger.info(
            "Reconciliando %s posicao(oes) com a exchange...",
            len(self.risk.positions),
        )
        now = datetime.now(timezone.utc)
        symbols = sorted(self.risk.positions)
        tickers = await self.exchange.fetch_tickers(symbols)
        for symbol, position in list(self.risk.positions.items()):
            ticker = tickers.get(symbol)
            if ticker is None:
                ticker = await self.exchange.fetch_ticker(symbol)
            last_price = _ticker_price(ticker) or position.entry_price
            stop_order: OrderResult | None = None
            if position.stop_order_id:
                stop_order = await self.exchange.fetch_order(
                    symbol, order_id=position.stop_order_id
                )
            if stop_order is None:
                stop_order = await self._find_bot_stop(symbol)
                if stop_order is not None:
                    position.stop_order_id = stop_order.id
                    self.risk.save()
                    logger.error(
                        "Reconciliacao: stop de %s readotado (id=%s).",
                        symbol,
                        stop_order.id,
                    )
            if stop_order is not None and stop_order.filled > 0:
                logger.info(
                    "Reconciliacao: stop de %s ja executado; registrando saida.",
                    symbol,
                )
                await self._handle_stop_exit(
                    position, stop_order, last_price, now
                )
                continue
            base = self.exchange.base_currency(symbol)
            balance = await self.exchange.fetch_balance()
            total = self.exchange.balance_total(balance, base)
            dust = self._dust(symbol)
            if total <= dust:
                close = self.risk.record_close(
                    symbol,
                    last_price,
                    "reconciliacao_sem_saldo",
                    exit_amount=position.amount,
                    now=now.timestamp(),
                )
                self._log_trade_close(close)
                continue
            if total < position.amount * 0.999:
                ratio = total / position.amount
                position.amount = total
                position.entry_notional *= ratio
                position.entry_fee_quote *= ratio
                self.risk.save()
                logger.critical(
                    "Reconciliacao: saldo de %s menor que o estado; ajustado.",
                    symbol,
                )
            if stop_order is not None and stop_order.is_open:
                if self._stop_order_is_protective(stop_order, position.amount):
                    logger.info(
                        "Reconciliacao: posicao %s protegida por stop %s.",
                        symbol,
                        stop_order.id,
                    )
                    continue
                logger.critical(
                    "Reconciliacao: stop de %s nao cobre a posicao inteira.",
                    symbol,
                )
                if last_price <= position.stop_price:
                    await self._force_exit(
                        position, "reconciliacao_stop_incompleto", now, last_price
                    )
                else:
                    await self._restore_stop_only(position, last_price, now)
                continue
            if last_price <= position.stop_price:
                await self._force_exit(
                    position, "reconciliacao_abaixo_do_stop", now, last_price
                )
            else:
                try:
                    await self._restore_stop_only(position, last_price, now)
                except Exception as exc:
                    raise FatalExecutionError(
                        f"reconciliacao nao conseguiu proteger {symbol}"
                    ) from exc
        logger.info("Reconciliacao de posicoes concluida.")


async def run_check(settings: Settings) -> int:
    exchange: ExchangeClient | None = None
    try:
        exchange = ExchangeClient(settings)
        await exchange.initialize()
        for symbol in settings.symbols:
            candles = await exchange.fetch_ohlcv(
                symbol, settings.timeframe, settings.ohlcv_limit + 1
            )
            if len(candles) < 60:
                raise ExchangeConfigurationError(
                    f"Candles insuficientes para {symbol}: {len(candles)}"
                )
            ticker = await exchange.fetch_ticker(symbol)
            last_price = _ticker_price(ticker)
            if last_price <= 0:
                raise ExchangeConfigurationError(
                    f"Preco invalido retornado para {symbol}."
                )
            strategy = Strategy(settings)
            signal = strategy.evaluate(
                symbol,
                candles,
                ticker,
                datetime.now(timezone.utc),
            )
            logger.info(
                "CHECK %s: candles=%s ultimo=%.8f sinal=%s score=%s/%s %s",
                symbol,
                len(candles),
                last_price,
                signal.action.value,
                signal.score,
                signal.max_score,
                signal.reasons[0],
            )
        permission_status = (
            "permissoes validadas"
            if exchange.permissions_verified
            else "permissoes nao verificadas neste modo"
        )
        logger.info(
            "CHECK OK: conexao, pares, candles, indicadores e %s.",
            permission_status,
        )
        return 0
    except (SecurityViolationError, ExchangeConfigurationError) as exc:
        logger.critical("CHECK FALHOU: %s", exc)
        return 78
    except Exception as exc:
        logger.error("CHECK FALHOU: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        if exchange is not None:
            await exchange.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bot de trading spot autonomo com gestao de risco rigorosa."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Valida configuracao, conexao, permissoes (quando aplicavel) e "
            "indicadores e encerra."
        ),
    )
    parser.add_argument(
        "--reset-halt",
        action="store_true",
        help="Remove o travamento do circuit breaker (use com o bot parado).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Forca modo dry-run, sem enviar ordens.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--web",
        action="store_true",
        help="Inicia o servidor de dashboard web em http://localhost:8080.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"ERRO DE CONFIGURACAO: {exc}", file=sys.stderr)
        return 78
    configure_logging(settings)

    if args.reset_halt:
        try:
            manager = RiskManager(settings)
        except StateStoreError as exc:
            logger.critical("Nao foi possivel carregar o estado: %s", exc)
            return 78
        if not manager.halted:
            logger.info("O bot nao esta travado; nada a resetar.")
            return 0
        manager.reset_halt()
        logger.warning(
            "Circuit breaker resetado em %s. Verifique saldo, ordens e "
            "posicoes antes de iniciar novamente.",
            settings.state_path,
        )
        return 0

    if args.check:
        return asyncio.run(run_check(settings))

    try:
        bot = TradingBot(settings, web_enabled=args.web)
    except ExchangeConfigurationError as exc:
        logger.critical("Erro de configuracao da exchange: %s", exc)
        return 78
    except StateStoreError as exc:
        logger.critical("Falha ao carregar estado: %s", exc)
        return 78
    except Exception as exc:
        logger.critical(
            "Falha ao inicializar o bot: %s: %s", type(exc).__name__, exc
        )
        return 1
    try:
        return asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.warning("Interrompido pelo usuario.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
