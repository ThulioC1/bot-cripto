from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent

TIMEFRAME_MS: Mapping[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} precisa ser um inteiro valido: {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} precisa ser um numero valido: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} precisa ser um numero finito: {raw!r}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} precisa ser true/false: {raw!r}")


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    return path.resolve()


def _read_secret(name: str) -> str:
    direct = os.getenv(name, "").strip()
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if direct and file_name:
        raise ValueError(f"Defina apenas {name} ou {name}_FILE, nunca os dois.")
    if not file_name:
        return direct
    path = Path(file_name).expanduser()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"Nao foi possivel ler {name}_FILE em {path}: {exc}") from exc


def _parse_symbols(raw: str) -> tuple[str, ...]:
    symbols = tuple(
        dict.fromkeys(part.strip().upper() for part in raw.split(",") if part.strip())
    )
    if not symbols:
        raise ValueError("SYMBOLS precisa conter ao menos um par, ex: BTC/USDT,ETH/USDT")
    return symbols


_INT_ENV = {
    "ohlcv_limit": "OHLCV_LIMIT",
    "scan_interval_seconds": "SCAN_INTERVAL_SECONDS",
    "log_max_bytes": "LOG_MAX_BYTES",
    "log_backup_count": "LOG_BACKUP_COUNT",
    "max_open_positions": "MAX_OPEN_POSITIONS",
    "max_trades_per_day": "MAX_TRADES_PER_DAY",
    "cooldown_after_loss_minutes": "COOLDOWN_AFTER_LOSS_MINUTES",
    "ema_fast": "EMA_FAST",
    "ema_slow": "EMA_SLOW",
    "rsi_period": "RSI_PERIOD",
    "macd_fast": "MACD_FAST",
    "macd_slow": "MACD_SLOW",
    "macd_signal": "MACD_SIGNAL",
    "atr_period": "ATR_PERIOD",
    "bb_period": "BB_PERIOD",
    "volume_ma_period": "VOLUME_MA_PERIOD",
    "breakout_lookback": "BREAKOUT_LOOKBACK",
    "entry_min_score": "ENTRY_MIN_SCORE",
    "api_timeout_ms": "API_TIMEOUT_MS",
    "max_retries": "MAX_RETRIES",
}

_FLOAT_ENV = {
    "paper_equity": "PAPER_EQUITY",
    "max_position_pct": "MAX_POSITION_PCT",
    "risk_per_trade_pct": "RISK_PER_TRADE_PCT",
    "max_total_exposure_pct": "MAX_TOTAL_EXPOSURE_PCT",
    "max_daily_drawdown_pct": "MAX_DAILY_DRAWDOWN_PCT",
    "min_quote_reserve_pct": "MIN_QUOTE_RESERVE_PCT",
    "min_order_quote": "MIN_ORDER_QUOTE",
    "fee_rate": "FEE_RATE",
    "rsi_min": "RSI_MIN",
    "rsi_max": "RSI_MAX",
    "bb_std": "BB_STD",
    "min_volume_ratio": "MIN_VOLUME_RATIO",
    "min_24h_quote_volume": "MIN_24H_QUOTE_VOLUME",
    "min_atr_pct": "MIN_ATR_PCT",
    "max_atr_pct": "MAX_ATR_PCT",
    "stop_atr_mult": "STOP_ATR_MULT",
    "min_stop_pct": "MIN_STOP_PCT",
    "max_stop_pct": "MAX_STOP_PCT",
    "tp_rr": "TP_RR",
    "max_last_candle_gain_pct": "MAX_LAST_CANDLE_GAIN_PCT",
    "stop_limit_offset_pct": "STOP_LIMIT_OFFSET_PCT",
    "trailing_atr_mult": "TRAILING_ATR_MULT",
    "breakeven_after_r": "BREAKEVEN_AFTER_R",
    "min_stop_move_pct": "MIN_STOP_MOVE_PCT",
    "retry_base_delay": "RETRY_BASE_DELAY",
}

_BOOL_ENV = {
    "sandbox": "SANDBOX",
    "dry_run": "DRY_RUN",
    "telegram_enabled": "TELEGRAM_ENABLED",
    "circuit_breaker_flatten": "CIRCUIT_BREAKER_FLATTEN",
    "enforce_api_permissions": "ENFORCE_API_PERMISSIONS",
    "api_permission_check_strict": "API_PERMISSION_CHECK_STRICT",
    "enable_trailing_stop": "ENABLE_TRAILING_STOP",
}

_STR_ENV = {
    "bot_name": "BOT_NAME",
    "exchange_id": "EXCHANGE_ID",
    "log_level": "LOG_LEVEL",
    "state_file": "STATE_FILE",
    "stop_order_type": "STOP_ORDER_TYPE",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}


@dataclass(frozen=True)
class Settings:
    bot_name: str = "KryptoBot"
    exchange_id: str = "binance"
    sandbox: bool = False
    dry_run: bool = True
    api_key: str = ""
    api_secret: str = ""
    confirm_live_trading: str = ""
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    quote_currency: str = "USDT"
    timeframe: str = "15m"
    ohlcv_limit: int = 300
    scan_interval_seconds: int = 300
    data_dir: Path = APP_DIR / "data"
    log_dir: Path = APP_DIR / "logs"
    state_file: str = "state.json"
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    paper_equity: float = 1_000.0
    max_position_pct: float = 5.0
    risk_per_trade_pct: float = 1.0
    max_total_exposure_pct: float = 15.0
    max_daily_drawdown_pct: float = 3.0
    max_open_positions: int = 3
    max_trades_per_day: int = 6
    min_quote_reserve_pct: float = 20.0
    min_order_quote: float = 10.0
    fee_rate: float = 0.001
    cooldown_after_loss_minutes: int = 60
    circuit_breaker_flatten: bool = True
    enforce_api_permissions: bool = True
    api_permission_check_strict: bool = True
    ema_fast: int = 21
    ema_slow: int = 55
    rsi_period: int = 14
    rsi_min: float = 50.0
    rsi_max: float = 68.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    volume_ma_period: int = 20
    breakout_lookback: int = 20
    min_volume_ratio: float = 1.1
    min_24h_quote_volume: float = 10_000_000.0
    min_atr_pct: float = 0.5
    max_atr_pct: float = 6.0
    stop_atr_mult: float = 2.0
    min_stop_pct: float = 1.5
    max_stop_pct: float = 4.0
    tp_rr: float = 2.0
    entry_min_score: int = 4
    max_last_candle_gain_pct: float = 6.0
    stop_order_type: str = "stop_loss"
    stop_limit_offset_pct: float = 2.0
    enable_trailing_stop: bool = True
    trailing_atr_mult: float = 2.5
    breakeven_after_r: float = 1.0
    min_stop_move_pct: float = 0.3
    api_timeout_ms: int = 30_000
    max_retries: int = 4
    retry_base_delay: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "quote_currency", self.quote_currency.strip().upper())
        object.__setattr__(self, "timeframe", self.timeframe.strip())
        object.__setattr__(self, "log_level", self.log_level.strip().upper())
        object.__setattr__(self, "stop_order_type", self.stop_order_type.strip().lower())
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "log_dir", Path(self.log_dir))

        if not self.bot_name.strip():
            raise ValueError("BOT_NAME nao pode ser vazio.")
        if not self.exchange_id.strip():
            raise ValueError("EXCHANGE_ID nao pode ser vazio.")
        if self.timeframe not in TIMEFRAME_MS:
            raise ValueError(
                f"TIMEFRAME invalido: {self.timeframe}. Use um de {sorted(TIMEFRAME_MS)}."
            )
        if not self.symbols:
            raise ValueError("SYMBOLS nao pode ser vazio.")
        suffix = f"/{self.quote_currency}"
        for symbol in self.symbols:
            if "/" not in symbol or not symbol.endswith(suffix):
                raise ValueError(
                    f"Par invalido: {symbol}. Todos os pares devem terminar com {suffix}."
                )
        if self.ohlcv_limit < 150 or self.ohlcv_limit > 1000:
            raise ValueError("OHLCV_LIMIT deve estar entre 150 e 1000.")
        if self.ohlcv_limit < self.ema_slow + 30:
            raise ValueError("OHLCV_LIMIT precisa ser ao menos EMA_SLOW + 30.")
        if self.scan_interval_seconds < 30 or self.scan_interval_seconds > 86_400:
            raise ValueError("SCAN_INTERVAL_SECONDS deve estar entre 30 e 86400.")

        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL invalido.")
        if self.log_max_bytes < 10_000:
            raise ValueError("LOG_MAX_BYTES deve ser >= 10000.")
        if self.log_backup_count < 1:
            raise ValueError("LOG_BACKUP_COUNT deve ser >= 1.")
        if not self.state_file.endswith(".json") or any(
            separator in self.state_file for separator in ("/", "\\", "..")
        ):
            raise ValueError("STATE_FILE deve ser um nome de arquivo .json simples.")

        if self.dry_run:
            if self.paper_equity <= 0:
                raise ValueError("PAPER_EQUITY deve ser > 0.")
        else:
            if not self.api_key or not self.api_secret:
                raise ValueError(
                    "EXCHANGE_API_KEY e EXCHANGE_API_SECRET sao obrigatorias quando DRY_RUN=false."
                )
            if not self.sandbox and self.confirm_live_trading.strip().upper() != "YES":
                raise ValueError(
                    "Trading real bloqueado. Defina CONFIRM_LIVE_TRADING=YES apos ler o README."
                )

        if self.telegram_enabled and (
            not self.telegram_bot_token or not self.telegram_chat_id
        ):
            raise ValueError(
                "TELEGRAM_ENABLED=true exige TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID."
            )

        if not 0.5 <= self.max_position_pct <= 10.0:
            raise ValueError("MAX_POSITION_PCT deve estar entre 0.5 e 10 (limite duro).")
        if not 0.1 <= self.risk_per_trade_pct <= 2.0:
            raise ValueError("RISK_PER_TRADE_PCT deve estar entre 0.1 e 2 (limite duro).")
        if not self.max_position_pct <= self.max_total_exposure_pct <= 100.0:
            raise ValueError(
                "MAX_TOTAL_EXPOSURE_PCT deve ser >= MAX_POSITION_PCT e <= 100."
            )
        if not 1.0 <= self.max_daily_drawdown_pct <= 5.0:
            raise ValueError(
                "MAX_DAILY_DRAWDOWN_PCT deve estar entre 1 e 5 (limite duro de seguranca)."
            )
        if not 1 <= self.max_open_positions <= 10:
            raise ValueError("MAX_OPEN_POSITIONS deve estar entre 1 e 10.")
        if not 1 <= self.max_trades_per_day <= 100:
            raise ValueError("MAX_TRADES_PER_DAY deve estar entre 1 e 100.")
        if not 0.0 <= self.min_quote_reserve_pct <= 90.0:
            raise ValueError("MIN_QUOTE_RESERVE_PCT deve estar entre 0 e 90.")
        if self.min_order_quote <= 0:
            raise ValueError("MIN_ORDER_QUOTE deve ser > 0.")
        if not 0.0 <= self.fee_rate <= 0.01:
            raise ValueError("FEE_RATE deve estar entre 0 e 0.01.")
        if not 0 <= self.cooldown_after_loss_minutes <= 10_080:
            raise ValueError("COOLDOWN_AFTER_LOSS_MINUTES deve estar entre 0 e 10080.")

        if self.ema_fast < 2 or self.ema_slow < 3 or self.ema_fast >= self.ema_slow:
            raise ValueError("Exige EMA_FAST >= 2 e EMA_SLOW > EMA_FAST.")
        if self.rsi_period < 2 or not 1.0 <= self.rsi_min < self.rsi_max <= 80.0:
            raise ValueError("RSI invalido: exige 1 <= RSI_MIN < RSI_MAX <= 80.")
        if (
            self.macd_fast < 2
            or self.macd_slow <= self.macd_fast
            or self.macd_signal < 2
        ):
            raise ValueError("MACD invalido: exige MACD_SLOW > MACD_FAST >= 2 e MACD_SIGNAL >= 2.")
        if self.atr_period < 2 or not 5 <= self.bb_period <= 200:
            raise ValueError("ATR_PERIOD >= 2 e BB_PERIOD entre 5 e 200.")
        if not 0.5 <= self.bb_std <= 5.0:
            raise ValueError("BB_STD deve estar entre 0.5 e 5.")
        if self.volume_ma_period < 5 or self.breakout_lookback < 5:
            raise ValueError("VOLUME_MA_PERIOD e BREAKOUT_LOOKBACK devem ser >= 5.")
        if not 0.0 <= self.min_volume_ratio <= 10.0:
            raise ValueError("MIN_VOLUME_RATIO deve estar entre 0 e 10.")
        if self.min_24h_quote_volume < 0:
            raise ValueError("MIN_24H_QUOTE_VOLUME deve ser >= 0.")
        if not 0.05 <= self.min_atr_pct < self.max_atr_pct <= 20.0:
            raise ValueError("Exige 0.05 <= MIN_ATR_PCT < MAX_ATR_PCT <= 20.")
        if not 0.5 <= self.stop_atr_mult <= 10.0:
            raise ValueError("STOP_ATR_MULT deve estar entre 0.5 e 10.")
        if not 0.2 <= self.min_stop_pct <= self.max_stop_pct <= 15.0:
            raise ValueError("Exige 0.2 <= MIN_STOP_PCT <= MAX_STOP_PCT <= 15.")
        if not 1.5 <= self.tp_rr <= 10.0:
            raise ValueError(
                "TP_RR deve ser >= 1.5 para garantir risco-retorno assimetrico (max 10)."
            )
        if not 1 <= self.entry_min_score <= 7:
            raise ValueError("ENTRY_MIN_SCORE deve estar entre 1 e 7.")
        if not 0.5 <= self.max_last_candle_gain_pct <= 20.0:
            raise ValueError("MAX_LAST_CANDLE_GAIN_PCT deve estar entre 0.5 e 20.")

        if self.stop_order_type not in {"stop_loss", "stop_loss_limit"}:
            raise ValueError("STOP_ORDER_TYPE deve ser 'stop_loss' ou 'stop_loss_limit'.")
        if not 0.1 <= self.stop_limit_offset_pct <= 10.0:
            raise ValueError("STOP_LIMIT_OFFSET_PCT deve estar entre 0.1 e 10.")
        if not 0.5 <= self.trailing_atr_mult <= 10.0:
            raise ValueError("TRAILING_ATR_MULT deve estar entre 0.5 e 10.")
        if not 0.5 <= self.breakeven_after_r <= 5.0:
            raise ValueError("BREAKEVEN_AFTER_R deve estar entre 0.5 e 5.")
        if not 0.05 <= self.min_stop_move_pct <= 5.0:
            raise ValueError("MIN_STOP_MOVE_PCT deve estar entre 0.05 e 5.")

        if not 5_000 <= self.api_timeout_ms <= 120_000:
            raise ValueError("API_TIMEOUT_MS deve estar entre 5000 e 120000.")
        if not 0 <= self.max_retries <= 10:
            raise ValueError("MAX_RETRIES deve estar entre 0 e 10.")
        if not 0.2 <= self.retry_base_delay <= 30.0:
            raise ValueError("RETRY_BASE_DELAY deve estar entre 0.2 e 30.")

    @property
    def timeframe_ms(self) -> int:
        return TIMEFRAME_MS[self.timeframe]

    @property
    def state_path(self) -> Path:
        if self.dry_run:
            name = "state.dryrun.json"
        elif self.sandbox:
            name = "state.testnet.json"
        else:
            name = self.state_file
        return self.data_dir / name

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(APP_DIR / ".env", override=False)
        defaults = cls()
        values: dict[str, Any] = {}

        for attribute, env_name in _STR_ENV.items():
            values[attribute] = _env_str(env_name, getattr(defaults, attribute))
        for attribute, env_name in _INT_ENV.items():
            values[attribute] = _env_int(env_name, getattr(defaults, attribute))
        for attribute, env_name in _FLOAT_ENV.items():
            values[attribute] = _env_float(env_name, getattr(defaults, attribute))
        for attribute, env_name in _BOOL_ENV.items():
            values[attribute] = _env_bool(env_name, getattr(defaults, attribute))

        values["symbols"] = _parse_symbols(
            os.getenv("SYMBOLS", ",".join(defaults.symbols))
        )
        values["quote_currency"] = _env_str("QUOTE_CURRENCY", defaults.quote_currency)
        values["timeframe"] = _env_str("TIMEFRAME", defaults.timeframe)
        values["data_dir"] = _env_path("DATA_DIR", defaults.data_dir)
        values["log_dir"] = _env_path("LOG_DIR", defaults.log_dir)
        values["api_key"] = _read_secret("EXCHANGE_API_KEY")
        values["api_secret"] = _read_secret("EXCHANGE_API_SECRET")
        values["telegram_bot_token"] = _read_secret("TELEGRAM_BOT_TOKEN")
        values["confirm_live_trading"] = _env_str("CONFIRM_LIVE_TRADING", "")
        values["telegram_chat_id"] = _env_str("TELEGRAM_CHAT_ID", "")

        return cls(**values)
