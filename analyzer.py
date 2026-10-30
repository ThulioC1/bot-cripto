from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone
from typing import Any

from config import Settings


class StatisticsEngine:
    """Motor de estatísticas para análise de desempenho da estratégia."""

    def __init__(self, settings: Settings, history_size: int = 500) -> None:
        self.settings = settings
        self.history_size = history_size

        # Histórico de sinais e preços
        self.signals_history: deque[dict] = deque(maxlen=history_size)
        self.prices_history: deque[float] = deque(maxlen=history_size)
        self.returns_history: deque[float] = deque(maxlen=history_size)

        # Métricas acumuladas
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_profit = 0.0
        self.total_loss = 0.0
        self.profit_factor_num = 0.0  # Lucro bruto total
        self.profit_factor_den = 0.0  # Perda bruta total

        # Estatísticas de sequência
        self.current_win_streak = 0
        self.current_loss_streak = 0
        self.max_win_streak = 0
        self.max_loss_streak = 0

        # Expected Value
        self.expected_value = 0.0

        # Data inicial do histórico
        self.start_date: datetime | None = None

    def update_signal(self, signal: dict[str, Any], price: float, timestamp: datetime) -> None:
        """Atualiza histórico com novo sinal gerado."""
        entry = {
            "timestamp": timestamp,
            "price": price,
            "signal": signal,
        }
        self.signals_history.append(entry)

        # Calcular retorno se tivéssemos entrado
        if self.start_date is None:
            self.start_date = timestamp

        self._calculate_metrics_from_signal(entry)

    def _calculate_metrics_from_signal(self, entry: dict[str, Any]) -> None:
        """Calcula retornos baseados no sinal e atualiza métricas."""
        entry_price = entry["price"]

        # Calcular retorno percentual em relação ao último preço conhecido
        if len(self.prices_history) >= 2:
            prev_price = self.prices_history[-1]
            if prev_price > 0:
                ret = (entry_price - prev_price) / prev_price
                self.returns_history.append(ret)

                # Atualizar sequências baseadas no retorno
                if ret > 0:
                    self.current_win_streak += 1
                    self.current_loss_streak = 0
                    self.winning_trades += 1
                elif ret < 0:
                    self.current_loss_streak += 1
                    self.current_win_streak = 0
                    self.losing_trades += 1
                else:
                    self.current_win_streak = max(0, self.current_win_streak - 1)
                    self.current_loss_streak = max(0, self.current_loss_streak - 1)

                # Atualizar máximas sequências
                self.max_win_streak = max(self.max_win_streak, self.current_win_streak)
                self.max_loss_streak = max(self.max_loss_streak, self.current_loss_streak)

                # Acumular lucros/perdas
                if ret > 0:
                    self.total_profit += ret
                elif ret < 0:
                    self.total_loss += ret  # negativo

        self.total_trades += 1

    def add_price(self, price: float, timestamp: datetime) -> None:
        """Adiciona preço ao histórico (chamado a cada varredura)."""
        self.prices_history.append(price)

    def calculate_win_rate(self) -> float:
        """Calcula win rate percentual."""
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100.0

    def calculate_profit_factor(self) -> float:
        """Calcula profit factor: lucro bruto / perda bruta absolute."""
        if self.profit_factor_den == 0:
            if self.profit_factor_num > 0:
                return float("inf")
            return 0.0
        return abs(self.profit_factor_num / self.profit_factor_den)

    def calculate_expected_value(self, risk_per_trade: float = 1.0) -> float:
        """
        Calcula Expected Value (EV) percentual por trade.
        EV = (Prob_win * Avg_win) - (Prob_loss * Avg_loss)
        """
        if self.total_trades == 0:
            return 0.0

        win_rate = self.calculate_win_rate() / 100.0
        loss_rate = 1.0 - win_rate

        # Médias de retorno
        winning_returns = [r for r in self.returns_history if r > 0]
        losing_returns = [r for r in self.returns_history if r < 0]

        avg_win = sum(winning_returns) / len(winning_returns) if winning_returns else 0.0
        avg_loss = abs(sum(losing_returns) / len(losing_returns)) if losing_returns else 0.0

        ev = (win_rate * avg_win) - (loss_rate * avg_loss)
        return ev

    def calculate_volatility_projection(self, periods: int = 3) -> dict[str, float]:
        """
        Projeta volatilidade baseada em dados históricos.
        Retorna intervalo de mudança percentual esperada.
        """
        if len(self.returns_history) < 10:
            return {"min": 0.0, "max": 0.0, "avg": 0.0}

        # Calcular desvios padrão dos retornos recentes
        recent_returns = list(self.returns_history)[-periods * 10 :]
        if not recent_returns:
            return {"min": 0.0, "max": 0.0, "avg": 0.0}

        # Desvio padrão simples
        mean_ret = sum(recent_returns) / len(recent_returns)
        variance = sum((r - mean_ret) ** 2 for r in recent_returns) / len(recent_returns)
        std_dev = math.sqrt(variance)

        # Projetar para múltiplos periods (usando 1 std dev)
        # Em mercados crypto, ~68% dos movimentos ficam dentro de 1 std dev
        projected_move = std_dev * math.sqrt(periods)  # Escala por tempo

        # Converter para porcentagem
        projected_pct = projected_move * 100.0

        # Intervalo de confiança aproximado ( +/- 1 std dev )
        return {
            "min": round(-projected_pct, 2),
            "max": round(projected_pct, 2),
            "avg": round(abs(projected_pct) * 0.6, 2),  # Mediana aproximada
        }

    def calculate_probability_direction(self, periods: int = 5) -> dict[str, float]:
        """
        Calcula probabilidade de direção baseada em dados históricos.
        Ex: "Qual a chance de o preço subir nas próximas 5 velas?"
        """
        if len(self.returns_history) < 20:
            return {"prob_up": 0.0, "prob_down": 0.0, "confidence": "low"}

        # Contar quantos períodos tiveram retorno positivo nas próximas N velas
        positive_future = 0
        total_comparable = 0

        returns_list = list(self.returns_history)
        for i in range(len(returns_list) - periods):
            # Retorno acumulado nos próximos 'periods' candles
            future_ret = sum(returns_list[i : i + periods])
            if future_ret > 0:
                positive_future += 1
            total_comparable += 1

        if total_comparable == 0:
            return {"prob_up": 0.0, "prob_down": 0.0, "confidence": "low"}

        prob_up = (positive_future / total_comparable) * 100.0
        prob_down = 100.0 - prob_up

        # Nível de confiança baseado no volume de dados
        confidence = "high" if total_comparable > 100 else "medium" if total_comparable > 30 else "low"

        return {
            "prob_up": round(prob_up, 1),
            "prob_down": round(prob_down, 1),
            "confidence": confidence,
        }

    def get_summary(self) -> dict[str, Any]:
        """Retorna resumo estatístico completo."""
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.calculate_win_rate(), 1),
            "profit_factor": round(self.calculate_profit_factor(), 2),
            "expected_value": round(self.calculate_expected_value(), 4),
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "current_win_streak": self.current_win_streak,
            "current_loss_streak": self.current_loss_streak,
            "total_profit_pct": round(self.total_profit * 100, 2) if self.total_trades > 0 else 0.0,
            "total_loss_pct": round(abs(self.total_loss) * 100, 2) if self.total_loss > 0 else 0.0,
            "data_points": len(self.returns_history),
        }

    def reset(self) -> None:
        """Reseta todas as estatísticas."""
        self.__init__(self.settings, self.history_size)


async def analyze_recent_market(
    exchange, symbol: str, timeframe: str, settings: Settings
) -> dict[str, Any]:
    """
    Analisa dados de mercado recentes e retorna métricas técnicas
    para o bot decidir entrada.
    """
    try:
        # Buscar candles recentes
        ohlcv = await exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, limit=min(100, settings.ohlcv_limit)
        )

        if not ohlcv or len(ohlcv) < 10:
            return {"error": "Dados insuficientes para análise"}

        # Converter para estrutura útil
        candles = []
        for candle in ohlcv:
            timestamp = datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc)
            candles.append(
                {
                    "timestamp": timestamp,
                    "open": candle[1],
                    "high": candle[2],
                    "low": candle[3],
                    "close": candle[4],
                    "volume": candle[5],
                }
            )

        # Calcular retornos simples
        closes = [c["close"] for c in candles]
        returns = []
        for i in range(1, len(closes)):
            ret = (closes[i] - closes[i - 1]) / closes[i - 1]
            returns.append(ret)

        if not returns:
            return {"error": "Não foi possível calcular retornos"}

        # Métricas básicas
        avg_return = sum(returns) / len(returns)
        volatility = math.sqrt(sum((r - avg_return) ** 2 for r in returns) / len(returns))

        # Contar quantos retornos foram positivos
        positive_count = sum(1 for r in returns if r > 0)
        positive_rate = (positive_count / len(returns)) * 100.0

        # Calcular ATR aproximado (usando high-low range)
        typical_range = []
        for c in candles:
            tr = c["high"] - c["low"]
            typical_range.append(tr)

        avg_true_range = sum(typical_range) / len(typical_range) if typical_range else 0

        # Projeção de volatilidade a partir de ATR
        last_close = closes[-1]
        atr_pct = (avg_true_range / last_close) * 100.0 if last_close > 0 else 0

        return {
            "avg_return_pct": round(avg_return * 100, 2),
            "volatility_pct": round(volatility * 100, 2),
            "positive_rate_pct": round(positive_rate, 1),
            "atr_pct": round(atr_pct, 2),
            "total_candles": len(candles),
            "price_change_last": round(
                (closes[-1] - closes[0]) / closes[0] * 100, 2
            ),
        }

    except Exception as e:
        return {"error": str(e)}


def print_statistics_report(stats: dict[str, Any]) -> None:
    """Imprime um relatório formatado das estatísticas atuais."""
    print("\n" + "=" * 50)
    print("RELATÓRIO ESTATÍSTICO DO BOT")
    print("=" * 50)
    print(f"Total de trades: {stats.get('total_trades', 0)}")
    print(f"Ganhos: {stats.get('winning_trades', 0)} | Perdas: {stats.get('losing_trades', 0)}")
    print(f"Win Rate: {stats.get('win_rate', 0)}%")
    print(f"Profit Factor: {stats.get('profit_factor', 0)}")
    print(f"Expected Value/trade: {stats.get('expected_value', 0):.4f}")
    print(f"Lucro total: {stats.get('total_profit_pct', 0)}%")
    print(f"Perda total: {stats.get('total_loss_pct', 0)}%")
    print(f"Maior sequência de vitórias: {stats.get('max_win_streak', 0)}")
    print(f"Maior sequência de derrotas: {stats.get('max_loss_streak', 0)}")
    print(f"Seq. atual - Vitórias: {stats.get('current_win_streak', 0)} | Derrotas: {stats.get('current_loss_streak', 0)}")
    print(f"Pontos de dados: {stats.get('data_points', 0)}")
    print("=" * 50 + "\n")


def print_market_analysis(market: dict[str, Any]) -> None:
    """Imprime análise do mercado atual."""
    if "error" in market:
        print(f"\n⚠️ Análise de mercado: {market['error']}")
        return

    print("\n" + "-" * 50)
    print("ANÁLISE DE MERCADO ATUAL")
    print("-" * 50)
    print(f"Candles analisados: {market.get('total_candles', 0)}")
    print(f"Média de retorno: {market.get('avg_return_pct', 0)}%")
    print(f"Volatilidade: {market.get('volatility_pct', 0)}%")
    print(f"Taxa de acertos históricos: {market.get('positive_rate_pct', 0)}%")
    print(f"ATR percentual: {market.get('atr_pct', 0)}%")
    print(f"Var. última período: {market.get('price_change_last', 0)}%")
    print("-" * 50 + "\n")