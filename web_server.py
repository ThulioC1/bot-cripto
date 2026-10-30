from __future__ import annotations

from datetime import datetime
from typing import Any

from aiohttp import web

from analyzer import StatisticsEngine, analyze_recent_market
from config import Settings
from risk_manager import RiskManager


class DashboardServer:
    """Servidor web opcional que expõe estatísticas, posições e decisões do bot."""

    def __init__(
        self,
        settings: Settings,
        statistics: StatisticsEngine,
        risk: RiskManager,
        exchange: Any = None,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.settings = settings
        self.statistics = statistics
        self.risk = risk
        self.exchange = exchange
        self.host = host
        self.port = port
        self.app = web.Application()
        self._setup_routes()
        self.runner: web.AppRunner | None = None
        self.recent_decisions: list[dict[str, Any]] = []

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/api/stats", self.api_stats)
        self.app.router.add_get("/api/positions", self.api_positions)
        self.app.router.add_get("/api/trades", self.api_trades)
        self.app.router.add_get("/api/market", self.api_market)
        self.app.router.add_get("/api/decisions", self.api_decisions)
        self.app.router.add_get("/api/health", self.api_health)

    def record_decision(self, decision: dict[str, Any]) -> None:
        entry = {"timestamp": datetime.now().isoformat(), **decision}
        self.recent_decisions.append(entry)
        if len(self.recent_decisions) > 200:
            self.recent_decisions = self.recent_decisions[-200:]

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse("static/dashboard.html")

    async def api_stats(self, request: web.Request) -> web.Response:
        return web.json_response(self.statistics.get_summary())

    async def api_positions(self, request: web.Request) -> web.Response:
        positions: dict[str, Any] = {}
        for symbol, pos in self.risk.positions.items():
            positions[symbol] = {
                "amount": pos.amount,
                "entry_price": pos.entry_price,
                "stop_price": pos.stop_price,
                "take_profit_price": pos.take_profit_price,
                "highest_price": pos.highest_price,
                "entry_notional": pos.entry_notional,
                "opened_at": pos.opened_at,
                "risk_per_unit": pos.risk_per_unit,
            }
        return web.json_response(positions)

    async def api_trades(self, request: web.Request) -> web.Response:
        trades = self.risk.state.trades[-100:]
        return web.json_response(trades)

    async def api_market(self, request: web.Request) -> web.Response:
        if self.exchange is None:
            return web.json_response({"error": "Exchange indisponível"})
        symbol = request.query.get("symbol", self.settings.symbols[0])
        try:
            market = await analyze_recent_market(
                self.exchange, symbol, self.settings.timeframe, self.settings
            )
            return web.json_response(market)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)})

    async def api_decisions(self, request: web.Request) -> web.Response:
        return web.json_response(self.recent_decisions[-100:])

    async def api_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
