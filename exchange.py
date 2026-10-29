from __future__ import annotations

import asyncio
import logging
import math
import random
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import ccxt
import ccxt.async_support as ccxt_async

from config import Settings

logger = logging.getLogger("bot.exchange")


class ExchangeConfigurationError(RuntimeError):
    pass


class SecurityViolationError(RuntimeError):
    pass


class OrderValidationError(RuntimeError):
    pass


class InsufficientBalanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketLimits:
    symbol: str
    min_amount: float
    min_cost: float
    amount_step: float
    price_tick: float


@dataclass(frozen=True)
class OrderResult:
    id: str
    client_order_id: str | None
    symbol: str
    side: str
    type: str
    status: str
    amount: float
    filled: float
    remaining: float
    price: float | None
    average: float | None
    cost: float
    fee_quote: float
    fee_base: float
    raw: Mapping[str, Any]

    @property
    def is_closed(self) -> bool:
        return self.status in {"closed", "filled"}

    @property
    def is_open(self) -> bool:
        return self.status in {"open", "new", "partially_filled"}

    @property
    def is_failed(self) -> bool:
        return self.status in {"canceled", "cancelled", "expired", "rejected"}


@dataclass(frozen=True)
class PermissionReport:
    withdrawals_enabled: bool
    reading_enabled: bool
    spot_trading_enabled: bool
    margin_enabled: bool
    futures_enabled: bool
    options_enabled: bool
    ip_restricted: bool


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return default


def _new_client_order_id(tag: str) -> str:
    token = secrets.token_hex(4).upper()
    return f"KRB{tag}{int(time.time() * 1000)}{token}"[:36]


class ExchangeClient:
    def __init__(self, settings: Settings) -> None:
        exchange_class = getattr(ccxt_async, settings.exchange_id, None)
        if exchange_class is None:
            raise ExchangeConfigurationError(
                f"Exchange '{settings.exchange_id}' nao e suportada pelo ccxt."
            )
        options: dict[str, Any] = {
            "defaultType": "spot",
            "adjustForTimeDifference": True,
        }
        if settings.exchange_id == "binance":
            options["recvWindow"] = 10_000
        params: dict[str, Any] = {
            "apiKey": settings.api_key,
            "secret": settings.api_secret,
            "enableRateLimit": True,
            "timeout": settings.api_timeout_ms,
            "options": options,
        }
        try:
            self._exchange = exchange_class(params)
        except Exception as exc:
            raise ExchangeConfigurationError(
                f"Falha ao inicializar {settings.exchange_id}: {type(exc).__name__}: {exc}"
            ) from exc
        self.settings = settings
        self._limits: dict[str, MarketLimits] = {}
        self._initialized = False
        self._permissions_verified = False

    @property
    def raw(self) -> Any:
        return self._exchange

    @property
    def permissions_verified(self) -> bool:
        return self._permissions_verified

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self.settings.sandbox:
            try:
                self._exchange.set_sandbox_mode(True)
            except Exception as exc:
                raise ExchangeConfigurationError(
                    f"A exchange nao suporta sandbox/testnet: {exc}"
                ) from exc
            logger.warning("SANDBOX/TESTNET ativo. Nenhum fundo real deve ser usado.")

        await self._retry_call(self._exchange.load_markets)
        if not self._exchange.markets:
            raise ExchangeConfigurationError("A exchange retornou lista de mercados vazia.")

        for symbol in self.settings.symbols:
            market = self._exchange.markets.get(symbol)
            if market is None:
                raise ExchangeConfigurationError(f"Par nao encontrado na exchange: {symbol}")
            if not market.get("spot"):
                raise ExchangeConfigurationError(f"Par nao e spot: {symbol}")
            if not market.get("active", True):
                raise ExchangeConfigurationError(f"Par inativo na exchange: {symbol}")
            if market.get("quote", "").upper() != self.settings.quote_currency:
                raise ExchangeConfigurationError(
                    f"Par {symbol} nao usa a moeda de cotacao {self.settings.quote_currency}."
                )
            self._limits[symbol] = self._build_limits(market)

        if not self.settings.dry_run:
            if (
                self.settings.enforce_api_permissions
                and not self.settings.sandbox
            ):
                report = await self.verify_api_permissions()
                self._permissions_verified = report is not None
            await self.fetch_balance()

        self._initialized = True
        summary = ", ".join(
            f"{symbol}(min={limits.min_cost:.2f} {self.settings.quote_currency})"
            for symbol, limits in self._limits.items()
        )
        logger.info("Mercados carregados: %s", summary)

    def _build_limits(self, market: Mapping[str, Any]) -> MarketLimits:
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        precision = market.get("precision") or {}
        precision_mode = getattr(self._exchange, "precisionMode", None)
        amount_step = 0.0
        price_tick = 0.0
        if precision_mode == ccxt.TICK_SIZE:
            amount_step = _as_float(precision.get("amount"))
            price_tick = _as_float(precision.get("price"))
        return MarketLimits(
            symbol=str(market.get("symbol")),
            min_amount=_as_float(amount_limits.get("min")),
            min_cost=_as_float(cost_limits.get("min")),
            amount_step=amount_step,
            price_tick=price_tick,
        )

    async def verify_api_permissions(self) -> PermissionReport | None:
        if self.settings.exchange_id != "binance":
            logger.warning(
                "Verificacao automatica de permissoes nao implementada para %s.",
                self.settings.exchange_id,
            )
            return None
        try:
            response = await self._retry_call(
                self._exchange.sapiGetAccountApiRestrictions
            )
        except Exception as exc:
            message = (
                "Nao foi possivel verificar as permissoes da API key "
                f"({type(exc).__name__}). Verifique manualmente que SAQUES estao desativados."
            )
            if self.settings.api_permission_check_strict:
                raise ExchangeConfigurationError(message) from exc
            logger.critical(message)
            return None

        if not isinstance(response, Mapping):
            raise ExchangeConfigurationError("Resposta invalida ao consultar permissoes da API.")

        report = PermissionReport(
            withdrawals_enabled=_as_bool(response.get("enableWithdrawals")),
            reading_enabled=_as_bool(response.get("enableReading")),
            spot_trading_enabled=_as_bool(
                response.get("enableSpotAndMarginTrading")
            ),
            margin_enabled=_as_bool(response.get("enableMargin")),
            futures_enabled=_as_bool(response.get("enableFutures")),
            options_enabled=_as_bool(response.get("enableVanillaOptions")),
            ip_restricted=_as_bool(response.get("ipRestrict")),
        )
        problems: list[str] = []
        if report.withdrawals_enabled:
            problems.append("SAQUES ESTAO HABILITADOS nesta API key")
        if not report.reading_enabled:
            problems.append("permissao de leitura desabilitada")
        if not report.spot_trading_enabled:
            problems.append("trade spot desabilitado")
        if report.margin_enabled:
            problems.append("margin habilitado")
        if report.futures_enabled:
            problems.append("futures habilitado")
        if report.options_enabled:
            problems.append("options habilitadas")
        if problems:
            raise SecurityViolationError(
                "API key insegura: " + "; ".join(problems) + ". Corrija na exchange e reinicie."
            )
        if not report.ip_restricted:
            logger.warning(
                "API key sem restricao de IP. Recomendado habilitar IP whitelist."
            )
        logger.info(
            "Permissoes da API validadas: saques=OFF, spot=ON, margin=OFF, futures=OFF."
        )
        return report

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (ccxt.NetworkError, ccxt.ExchangeNotAvailable)):
            return True
        if isinstance(exc, ccxt.ExchangeError):
            message = str(exc).lower()
            markers = (
                "429",
                "rate limit",
                "too many requests",
                "502",
                "503",
                "504",
                "temporarily unavailable",
                "internal error",
            )
            return any(marker in message for marker in markers)
        return False

    async def _retry_call(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                return await method(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._is_retryable(exc) or attempt >= attempts - 1:
                    raise
                delay = self.settings.retry_base_delay * (2**attempt)
                delay += random.uniform(0.0, self.settings.retry_base_delay)
                if isinstance(exc, ccxt.RateLimitExceeded):
                    delay = max(delay, 5.0)
                delay = min(delay, 60.0)
                logger.warning(
                    "Falha transitoria na exchange (%s). Tentativa %s/%s em %.1fs.",
                    type(exc).__name__,
                    attempt + 1,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("Falha inesperada no retry da exchange.")

    def floor_amount(self, symbol: str, amount: float) -> float:
        limits = self.market_limits(symbol)
        if amount <= 0:
            return 0.0
        if limits.amount_step > 0:
            steps = math.floor(amount / limits.amount_step + 1e-9)
            value = steps * limits.amount_step
            return float(f"{value:.12f}")
        try:
            return float(self._exchange.amount_to_precision(symbol, amount))
        except Exception:
            return float(f"{amount:.12f}")

    def round_price(self, symbol: str, price: float) -> float:
        try:
            return float(self._exchange.price_to_precision(symbol, price))
        except Exception:
            return float(f"{price:.12f}")

    def market_limits(self, symbol: str) -> MarketLimits:
        limits = self._limits.get(symbol)
        if limits is None:
            raise ExchangeConfigurationError(f"Limites desconhecidos para {symbol}.")
        return limits

    def base_currency(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def quote_currency(self, symbol: str) -> str:
        return symbol.split("/")[1]

    async def fetch_balance(self) -> Mapping[str, Any]:
        balance = await self._retry_call(self._exchange.fetch_balance)
        if not isinstance(balance, Mapping):
            raise ExchangeConfigurationError("Resposta de saldo invalida.")
        return balance

    @staticmethod
    def balance_free(balance: Mapping[str, Any], currency: str) -> float:
        section = balance.get("free") or {}
        return _as_float(section.get(currency))

    @staticmethod
    def balance_total(balance: Mapping[str, Any], currency: str) -> float:
        section = balance.get("total") or {}
        return _as_float(section.get(currency))

    async def fetch_ticker(self, symbol: str) -> Mapping[str, Any]:
        ticker = await self._retry_call(self._exchange.fetch_ticker, symbol)
        if not isinstance(ticker, Mapping):
            raise ExchangeConfigurationError(f"Ticker invalido para {symbol}.")
        return ticker

    async def fetch_tickers(
        self, symbols: Iterable[str]
    ) -> dict[str, Mapping[str, Any]]:
        tickers: dict[str, Mapping[str, Any]] = {}
        for symbol in symbols:
            try:
                tickers[symbol] = await self.fetch_ticker(symbol)
            except Exception as exc:
                logger.warning("Falha ao buscar ticker de %s: %s", symbol, type(exc).__name__)
        return tickers

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[list[float]]:
        rows = await self._retry_call(
            self._exchange.fetch_ohlcv, symbol, timeframe, None, limit
        )
        candles: list[list[float]] = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            timestamp = _as_float(row[0])
            open_price = _as_float(row[1])
            high = _as_float(row[2])
            low = _as_float(row[3])
            close = _as_float(row[4])
            volume = _as_float(row[5])
            if min(open_price, high, low, close) <= 0 or volume < 0:
                continue
            candles.append(
                [timestamp, open_price, high, low, close, volume]
            )
        if len(candles) < 2:
            logger.warning("Poucos candles para %s (%s).", symbol, len(candles))
        return candles

    async def fetch_order(
        self,
        symbol: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> OrderResult | None:
        if not order_id and not client_order_id:
            raise ValueError("Informe order_id ou client_order_id.")
        params: dict[str, Any] = {}
        if client_order_id:
            params["clientOrderId"] = client_order_id
        try:
            raw = await self._retry_call(
                self._exchange.fetch_order, order_id, symbol, params
            )
        except ccxt.OrderNotFound:
            return None
        return self._parse_order(raw, symbol)

    async def fetch_open_orders(self, symbol: str) -> list[OrderResult]:
        rows = await self._retry_call(
            self._exchange.fetch_open_orders, symbol, None, None, {}
        )
        orders: list[OrderResult] = []
        for row in rows or []:
            try:
                orders.append(self._parse_order(row, symbol))
            except Exception as exc:
                logger.warning(
                    "Ordem aberta ilegivel em %s ignorada: %s", symbol, type(exc).__name__
                )
        return orders

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._retry_call(
                self._exchange.cancel_order, order_id, symbol, {}
            )
            return True
        except ccxt.OrderNotFound:
            return False
        except ccxt.ExchangeError as exc:
            message = str(exc).lower()
            if "unknown order" in message or "not found" in message:
                return False
            raise

    async def cancel_bot_orders(
        self,
        symbols: Iterable[str],
        known_ids: Mapping[str, set[str]] | None = None,
    ) -> int:
        known_ids = known_ids or {}
        canceled = 0
        errors: list[Exception] = []
        for symbol in dict.fromkeys(symbols):
            try:
                open_orders = await self.fetch_open_orders(symbol)
            except Exception as exc:
                logger.error(
                    "Nao foi possivel listar ordens abertas de %s: %s",
                    symbol,
                    type(exc).__name__,
                )
                errors.append(exc)
                continue
            protected_ids = known_ids.get(symbol, set())
            for order in open_orders:
                is_bot_order = bool(
                    order.client_order_id
                    and order.client_order_id.upper().startswith("KRB")
                )
                if not is_bot_order and order.id not in protected_ids:
                    continue
                try:
                    if await self.cancel_order(symbol, order.id):
                        canceled += 1
                except Exception as exc:
                    logger.error(
                        "Falha ao cancelar ordem %s em %s: %s",
                        order.id,
                        symbol,
                        type(exc).__name__,
                    )
                    errors.append(exc)
        if errors:
            raise errors[0]
        return canceled

    async def _create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None,
        params: Mapping[str, Any],
        tag: str,
    ) -> OrderResult:
        if amount <= 0:
            raise OrderValidationError("Quantidade da ordem deve ser positiva.")
        request_params = dict(params)
        if "clientOrderId" not in request_params:
            request_params["clientOrderId"] = _new_client_order_id(tag)
        client_order_id = str(request_params["clientOrderId"])
        raw: Mapping[str, Any]
        try:
            raw = await self._retry_call(
                self._exchange.create_order,
                symbol,
                order_type,
                side,
                amount,
                price,
                request_params,
            )
        except ccxt.ExchangeError as exc:
            if "duplicate" in str(exc).lower():
                logger.warning(
                    "Ordem duplicada detectada para %s; recuperando estado.", symbol
                )
                raw = await self._retry_call(
                    self._exchange.fetch_order,
                    None,
                    symbol,
                    {"clientOrderId": client_order_id},
                )
            else:
                raise
        return self._parse_order(raw, symbol)

    async def _ensure_market_fill(
        self, symbol: str, order: OrderResult
    ) -> OrderResult:
        if order.filled > 0 or not order.is_open:
            return order
        current = order
        for _ in range(3):
            await asyncio.sleep(0.6)
            fetched = await self.fetch_order(symbol, order_id=order.id)
            if fetched is None:
                continue
            current = fetched
            if current.filled > 0 or not current.is_open:
                return current
        return current

    async def create_market_buy(
        self, symbol: str, quote_amount: float
    ) -> OrderResult:
        if quote_amount <= 0:
            raise OrderValidationError("Notional da compra deve ser positivo.")
        ticker = await self.fetch_ticker(symbol)
        reference_price = _as_float(ticker.get("ask")) or _as_float(
            ticker.get("last")
        )
        if reference_price <= 0:
            raise OrderValidationError(f"Sem preco valido para {symbol}.")
        limits = self.market_limits(symbol)
        minimum = max(limits.min_cost, self.settings.min_order_quote)
        if quote_amount < minimum * 0.99:
            raise OrderValidationError(
                f"Notional {quote_amount:.2f} abaixo do minimo {minimum:.2f}."
            )
        supports_quote_cost = bool(
            (self._exchange.options.get("createOrder") or {}).get("quoteOrderQty")
        )
        if supports_quote_cost:
            order = await self._create_order(
                symbol,
                "market",
                "buy",
                quote_amount,
                None,
                {"quoteOrderQty": quote_amount},
                "E",
            )
        else:
            amount = self.floor_amount(
                symbol, (quote_amount / reference_price) * 0.997
            )
            if amount < limits.min_amount:
                raise OrderValidationError(
                    f"Quantidade {amount} abaixo do minimo {limits.min_amount}."
                )
            if amount * reference_price < minimum * 0.99:
                raise OrderValidationError(
                    f"Notional calculado abaixo do minimo de {minimum:.2f}."
                )
            order = await self._create_order(
                symbol, "market", "buy", amount, None, {}, "E"
            )
        order = await self._ensure_market_fill(symbol, order)
        if order.filled <= 0:
            raise OrderValidationError(
                f"Compra de {symbol} aceita, mas sem execucao confirmada."
            )
        return order

    async def create_market_sell(
        self, symbol: str, base_amount: float
    ) -> OrderResult:
        if base_amount <= 0:
            raise OrderValidationError("Quantidade de venda deve ser positiva.")
        balance = await self.fetch_balance()
        base = self.base_currency(symbol)
        free = self.balance_free(balance, base)
        if free <= 0:
            raise InsufficientBalanceError(
                f"Saldo livre de {base} e zero; verifique ordens de stop abertas."
            )
        amount = self.floor_amount(symbol, min(base_amount, free))
        limits = self.market_limits(symbol)
        if amount < limits.min_amount:
            raise OrderValidationError(
                f"Quantidade {amount} abaixo do minimo {limits.min_amount}."
            )
        ticker = await self.fetch_ticker(symbol)
        reference_price = _as_float(ticker.get("bid")) or _as_float(
            ticker.get("last")
        )
        if amount * reference_price < limits.min_cost:
            raise OrderValidationError(
                f"Notional de venda abaixo do minimo {limits.min_cost:.2f}."
            )
        order = await self._create_order(
            symbol, "market", "sell", amount, None, {}, "X"
        )
        order = await self._ensure_market_fill(symbol, order)
        if order.filled <= 0:
            raise OrderValidationError(
                f"Venda de {symbol} aceita, mas sem execucao confirmada."
            )
        return order

    async def place_stop_loss(
        self, symbol: str, base_amount: float, stop_price: float
    ) -> OrderResult:
        if base_amount <= 0 or stop_price <= 0:
            raise OrderValidationError("Stop-loss exige quantidade e preco positivos.")
        amount = self.floor_amount(symbol, base_amount)
        limits = self.market_limits(symbol)
        if amount <= 0 or amount < limits.min_amount:
            raise OrderValidationError(
                "Quantidade do stop-loss abaixo do minimo da exchange."
            )
        trigger = self.round_price(symbol, stop_price)
        if trigger <= 0:
            raise OrderValidationError("Preco de gatilho do stop-loss invalido.")
        if self.settings.stop_order_type == "stop_loss":
            try:
                return await self._create_order(
                    symbol,
                    "stop_loss",
                    "sell",
                    amount,
                    None,
                    {"triggerPrice": trigger},
                    "S",
                )
            except (ccxt.InvalidOrder, ccxt.NotSupported) as exc:
                logger.warning(
                    "Stop market recusado em %s (%s); usando stop-limit.",
                    symbol,
                    type(exc).__name__,
                )
            except ccxt.ExchangeError as exc:
                message = str(exc).lower()
                if "order type" not in message and "not supported" not in message:
                    raise
                logger.warning(
                    "Stop market nao suportado em %s; usando stop-limit.", symbol
                )
        limit_price = self.round_price(
            symbol, trigger * (1 - self.settings.stop_limit_offset_pct / 100)
        )
        if limit_price <= 0 or limit_price >= trigger:
            raise OrderValidationError("Preco limite do stop-loss invalido.")
        return await self._create_order(
            symbol,
            "stop_loss_limit",
            "sell",
            amount,
            limit_price,
            {"triggerPrice": trigger, "timeInForce": "GTC"},
            "S",
        )

    def _parse_order(self, raw: Mapping[str, Any], symbol: str) -> OrderResult:
        if not isinstance(raw, Mapping):
            raise ExchangeConfigurationError("Resposta de ordem invalida.")
        order_id = raw.get("id") or (raw.get("info") or {}).get("orderId")
        if order_id is None:
            raise ExchangeConfigurationError("Ordem sem identificador retornada pela exchange.")
        status = str(raw.get("status") or "unknown").lower()
        if status == "new":
            status = "open"
        if status == "filled":
            status = "closed"
        amount = _as_float(raw.get("amount"))
        filled = _as_float(raw.get("filled"))
        remaining = _as_float(raw.get("remaining"))
        cost = _as_float(raw.get("cost"))
        average = _as_float(raw.get("average")) or None
        price = _as_float(raw.get("price")) or None
        if average is None and filled > 0 and cost > 0:
            average = cost / filled
        if cost <= 0 and average is not None and filled > 0:
            cost = average * filled
        if remaining <= 0 and amount > 0 and filled < amount:
            remaining = max(amount - filled, 0.0)
        reference_price = average or price or 0.0
        return OrderResult(
            id=str(order_id),
            client_order_id=(
                str(raw["clientOrderId"]) if raw.get("clientOrderId") else None
            ),
            symbol=str(raw.get("symbol") or symbol),
            side=str(raw.get("side") or ""),
            type=str(raw.get("type") or ""),
            status=status,
            amount=amount,
            filled=filled,
            remaining=remaining,
            price=price,
            average=average,
            cost=cost,
            fee_quote=self._extract_fee_quote(raw, reference_price, cost, symbol),
            fee_base=self._extract_fee_base(raw, symbol),
            raw=raw,
        )

    def _fee_items(self, raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        if not isinstance(raw, Mapping):
            return items

        def add_item(item: Any) -> None:
            if not isinstance(item, Mapping):
                return
            if not any(
                key in item
                for key in ("currency", "cost", "commission", "commissionAsset")
            ):
                return
            items.append(item)

        def add_container(container: Any) -> None:
            if isinstance(container, Mapping):
                add_item(container)
            elif isinstance(container, (list, tuple)):
                for item in container:
                    add_item(item)

        def trade_items(trade: Mapping[str, Any]) -> list[Mapping[str, Any]]:
            result: list[Mapping[str, Any]] = []
            if trade.get("fee") or trade.get("fees"):
                add_container(trade.get("fee"))
                add_container(trade.get("fees"))
                return result
            trade_info = trade.get("info")
            if isinstance(trade_info, Mapping):
                commission = trade_info.get("commission")
                asset = trade_info.get("commissionAsset")
                if commission is not None:
                    result.append({"currency": asset, "cost": commission})
                    return result
            commission = trade.get("commission")
            asset = trade.get("commissionAsset")
            if commission is not None:
                result.append({"currency": asset, "cost": commission})
            return result

        trades = raw.get("trades")
        if isinstance(trades, (list, tuple)) and trades:
            for trade in trades:
                if isinstance(trade, Mapping):
                    items.extend(trade_items(trade))
            return items

        fills = raw.get("fills")
        if isinstance(fills, (list, tuple)) and fills:
            for fill in fills:
                if isinstance(fill, Mapping):
                    if fill.get("fee") or fill.get("fees"):
                        add_container(fill.get("fee"))
                        add_container(fill.get("fees"))
                    else:
                        commission = fill.get("commission")
                        asset = fill.get("commissionAsset")
                        if commission is not None:
                            items.append({"currency": asset, "cost": commission})
                        else:
                            add_item(fill)
            return items

        add_container(raw.get("fee"))
        add_container(raw.get("fees"))

        info = raw.get("info")
        if isinstance(info, Mapping):
            add_container(info.get("fee"))
            add_container(info.get("fees"))
            fills = info.get("fills")
            if isinstance(fills, (list, tuple)):
                for fill in fills:
                    if isinstance(fill, Mapping):
                        if fill.get("fee") or fill.get("fees"):
                            add_container(fill.get("fee"))
                            add_container(fill.get("fees"))
                        else:
                            commission = fill.get("commission")
                            asset = fill.get("commissionAsset")
                            if commission is not None:
                                items.append({"currency": asset, "cost": commission})
                            else:
                                add_item(fill)
            commission = info.get("commission")
            asset = info.get("commissionAsset")
            if commission is not None:
                items.append({"currency": asset, "cost": commission})

        return items

    def _fee_currency(self, item: Mapping[str, Any]) -> Any:
        return item.get("currency", item.get("commissionAsset"))

    def _fee_cost(self, item: Mapping[str, Any]) -> Any:
        return item.get("cost", item.get("commission"))

    def _extract_fee_quote(
        self,
        raw: Mapping[str, Any],
        reference_price: float,
        fallback_cost: float,
        symbol: str,
    ) -> float:
        if not raw:
            return 0.0
        quote = self.quote_currency(symbol)
        base = self.base_currency(symbol)
        total = 0.0

        def add_fee(currency: Any, cost: Any) -> None:
            nonlocal total
            fee_cost = _as_float(cost)
            if fee_cost <= 0:
                return
            normalized = str(currency or "").upper()
            if normalized == quote:
                total += fee_cost
            elif normalized == base and reference_price > 0:
                total += fee_cost * reference_price

        for item in self._fee_items(raw):
            add_fee(self._fee_currency(item), self._fee_cost(item))
        if total <= 0:
            total = max(fallback_cost, 0.0) * self.settings.fee_rate
        return total

    def _extract_fee_base(self, raw: Mapping[str, Any], symbol: str) -> float:
        if not raw:
            return 0.0
        base = self.base_currency(symbol)
        total = 0.0

        def add_fee(currency: Any, cost: Any) -> None:
            nonlocal total
            fee_cost = _as_float(cost)
            if fee_cost > 0 and str(currency or "").upper() == base:
                total += fee_cost

        for item in self._fee_items(raw):
            add_fee(self._fee_currency(item), self._fee_cost(item))
        return total

    async def close(self) -> None:
        try:
            await self._exchange.close()
        except Exception as exc:
            logger.warning("Erro ao encerrar conexao com a exchange: %s", type(exc).__name__)
