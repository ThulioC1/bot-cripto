from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

import aiohttp

from config import Settings


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(settings: Settings) -> logging.Logger:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            settings.log_dir.chmod(0o700)
        except OSError:
            pass

    root = logging.getLogger()
    if any(getattr(handler, "_krypto_handler", False) for handler in root.handlers):
        return logging.getLogger("bot")

    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    formatter = UTCFormatter(_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(getattr(logging, settings.log_level, logging.INFO))
    console._krypto_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)

    log_path = settings.log_dir / "bot.log"
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(getattr(logging, settings.log_level, logging.INFO))
    file_handler._krypto_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    if os.name == "posix":
        try:
            log_path.touch(exist_ok=True)
            log_path.chmod(0o600)
        except OSError:
            pass

    return logging.getLogger("bot")


@dataclass(frozen=True)
class _QueuedAlert:
    text: str
    critical: bool


class TelegramLogHandler(logging.Handler):
    def __init__(self, alerter: AlertService) -> None:
        super().__init__(level=logging.ERROR)
        self._alerter = alerter
        self.setFormatter(UTCFormatter("%(levelname)s | %(name)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.name.startswith("bot"):
                return
            if getattr(record, "skip_telegram", False):
                return
            self._alerter.notify(self.format(record), critical=True)
        except Exception:
            self.handleError(record)


class AlertService:
    def __init__(self, settings: Settings) -> None:
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._bot_name = settings.bot_name
        self._enabled = bool(
            settings.telegram_enabled and self._token and self._chat_id
        )
        self._logger = logging.getLogger("bot.alerts")
        self._queue: asyncio.Queue[_QueuedAlert] | None = None
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._closed = False
        self._last_sent_at = 0.0
        self._dedupe: dict[str, float] = {}
        self._handler: TelegramLogHandler | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._queue = asyncio.Queue(maxsize=100)
        self._task = asyncio.create_task(self._worker(), name="telegram-alerts")
        self._logger.info("Servico de alertas Telegram iniciado.")

    def install_log_handler(self) -> None:
        if not self._enabled or self._handler is not None:
            return
        self._handler = TelegramLogHandler(self)
        logging.getLogger().addHandler(self._handler)

    def notify(self, text: str, critical: bool = False) -> None:
        if not self._enabled or self._queue is None or self._closed:
            return
        message = text.strip()
        if not message:
            return
        alert = _QueuedAlert(text=message, critical=critical)
        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                self._logger.warning(
                    "Fila do Telegram cheia; alerta descartado: %s",
                    dropped.text[:80],
                    extra={"skip_telegram": True},
                )
            except (asyncio.QueueEmpty, ValueError):
                pass
            try:
                self._queue.put_nowait(alert)
            except asyncio.QueueFull:
                self._logger.warning(
                    "Fila do Telegram cheia; alerta critico perdido.",
                    extra={"skip_telegram": True},
                )

    async def _worker(self) -> None:
        assert self._queue is not None
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "krypto-bot/1.0"},
        )
        try:
            while True:
                alert = await self._queue.get()
                try:
                    now = time.monotonic()
                    wait_for = 1.2 - (now - self._last_sent_at)
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                    key = f"{alert.critical}:{alert.text[:160]}"
                    last = self._dedupe.get(key, 0.0)
                    if time.monotonic() - last < 60:
                        continue
                    self._dedupe[key] = time.monotonic()
                    if len(self._dedupe) > 200:
                        cutoff = time.monotonic() - 300
                        self._dedupe = {
                            name: sent_at
                            for name, sent_at in self._dedupe.items()
                            if sent_at >= cutoff
                        }
                    await self._send(alert)
                    self._last_sent_at = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._logger.error(
                        "Falha ao processar alerta Telegram: %s",
                        type(exc).__name__,
                        extra={"skip_telegram": True},
                    )
                finally:
                    self._queue.task_done()
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def _send(self, alert: _QueuedAlert) -> None:
        if self._session is None:
            return
        prefix = "CRITICO" if alert.critical else "INFO"
        body = html.escape(alert.text)
        header = f"<b>{html.escape(self._bot_name)}</b> | {prefix} | {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC"
        payload = {
            "chat_id": self._chat_id,
            "text": f"{header}\n{body}"[:3900],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"

        for attempt in range(3):
            try:
                async with self._session.post(url, data=payload) as response:
                    if response.status == 200:
                        data = await response.json(content_type=None)
                        if isinstance(data, dict) and data.get("ok"):
                            return
                        self._logger.error(
                            "Telegram retornou HTTP 200 sem confirmacao de envio.",
                            extra={"skip_telegram": True},
                        )
                        return
                    if response.status == 429:
                        retry_after = 3
                        try:
                            data = await response.json(content_type=None)
                            retry_after = int(
                                data.get("parameters", {}).get("retry_after", 3)
                            )
                        except (ValueError, TypeError, aiohttp.ContentTypeError):
                            pass
                        await asyncio.sleep(min(max(retry_after, 1), 30))
                        continue
                    if response.status >= 500:
                        await asyncio.sleep(2**attempt)
                        continue
                    self._logger.error(
                        "Telegram recusou o alerta (HTTP %s).", response.status,
                        extra={"skip_telegram": True},
                    )
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= 2:
                    self._logger.error(
                        "Telegram indisponivel: %s", type(exc).__name__,
                        extra={"skip_telegram": True},
                    )
                    return
                await asyncio.sleep(2**attempt)

    async def stop(self) -> None:
        self._closed = True
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
        if not self._enabled:
            return
        if self._queue is not None and self._task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=8)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "Timeout ao esvaziar fila do Telegram.",
                    extra={"skip_telegram": True},
                )
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._queue = None
