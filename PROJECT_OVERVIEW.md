# KryptoBot - Project Overview

**Project Name:** KryptoBot  
**Version:** 1.0.0  
**Description:** Bot de trading automatizado para Binance Spot, escrito em Python com `ccxt`. Opera apenas comprado (long-only), sem margem, sem futuros e sem alavancagem.

## Visão Geral

O objetivo do projeto é executar uma estratégia conservadora com gestão de risco rígida:

- Entradas por confluência de EMA, RSI, MACD, Bollinger, ATR e volume
- Stop-loss obrigatório na exchange logo após a compra
- Take-profit monitorado pelo bot e executado a mercado
- Trailing stop opcional
- Circuit breaker por drawdown
- Limite de risco por operação, de posição e de exposição total
- Reconciliação de posições e stops após reinício
- Notificações opcionais por Telegram
- Supervisão por PM2 ou systemd

## Modos de Operação

| Modo | Variável de Ambiente | Descrição |
|------|----------------------|-----------|
| DRY-RUN | `DRY_RUN=true` | Sinais e dimensionamento são calculados, mas NENHUMA ordem é enviada |
| TESTNET | `SANDBOX=true`, `DRY_RUN=false` | Modo testnet da Binance sem fundos reais |
| LIVE | `DRY_RUN=false`, `CONFIRM_LIVE_TRADING=YES` | Ordens reais na Binance |

## Versão Atual

- **Versão da aplicação:** 1.0.0 (definida em `main.py:38`)
- **Versão do estado:** 1 (definido em `risk_manager.py:18`, `STATE_VERSION`)
- **Ultima alteração:** Registe todas as modificações neste arquivo

## Estrutura de Arquivos Principais

```
main.py              orquestração, loop, reconciliação e proteção
config.py            configuração e validação de limites
exchange.py          integração ccxt/Binance, retries e permissões
strategy.py          indicadores e sinal
risk_manager.py      sizing, estado, circuit breaker e trailing
logger.py            logs rotativos e Telegram
tests/               testes unitários
deploy/              configuração systemd
ecosystem.config.js  configuração PM2
.env.example         modelo de configuração
PROJECT_OVERVIEW.md  este arquivo - visão geral do projeto (ATUALIZE EM CADA ALTERACAO)
```

## Variáveis de Ambiente Principais

| Variável | Padrão | Observação |
|---|---|---|
| `DRY_RUN` | `true` | `false` envia ordens reais/testnet |
| `SANDBOX` | `false` | Ativa o modo testnet do `ccxt` |
| `CONFIRM_LIVE_TRADING` | `NO` | Precisa ser `YES` para operar live |
| `SYMBOLS` | `BTC/USDT,ETH/USDT,SOL/USDT` | Todos devem usar a moeda de cotação |
| `QUOTE_CURRENCY` | `USDT` | Moeda de caixa |
| `TIMEFRAME` | `15m` | `1m` até `1d` |
| `SCAN_INTERVAL_SECONDS` | `300` | Intervalo entre ciclos |
| `MAX_POSITION_PCT` | `5` | Limite duro de 10% |
| `RISK_PER_TRADE_PCT` | `1` | Limite duro de 2% |
| `MAX_TOTAL_EXPOSURE_PCT` | `15` | Exposição máxima da carteira |
| `MAX_DAILY_DRAWDOWN_PCT` | `3` | Limite duro entre 1% e 5% |
| `MAX_OPEN_POSITIONS` | `3` | Entre 1 e 10 |
| `MAX_TRADES_PER_DAY` | `6` | Limite diário de entradas |
| `TELEGRAM_ENABLED` | `false` | Exige token e chat id |

## Códigos de Saída

| Código | Significado | Supervisor |
|---|---|---|
| `0` | parada normal | não reinicia |
| `78` | configuração, autenticação, segurança, persistência ou circuit breaker | não reinicia |
| `1` | falha transitória ou estado de execução inseguro | reinicia |

## Logs e Estado

- `data/state.dryrun.json` — estado dry-run
- `data/state.testnet.json` — estado testnet
- `data/state.json` — estado live
- `*.json.bak` — backup automático
- `*.json.corrupt-*` — estado corrompido preservado para análise
- `logs/bot.log` — log rotativo
- `.env` — segredos e configuração local

## Instalação

```bash
# Em Linux/Mac
cd /opt/crypto-bot
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env

# No Windows PowerShell
py -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\pip.exe install -r requirements.txt
Copy-Item .env.example .env
```

## Execução

### Validação sem enviar ordens:
```bash
.venv/bin/python main.py --check
```

### Dry-run com loop completo:
```bash
.venv/bin/python main.py
```

### Forçar dry-run por linha de comando:
```bash
.venv/bin/python main.py --dry-run
```

### Resetar circuit breaker (bot parado):
```bash
.venv/bin/python main.py --reset-halt
```

## Execução com PM2

```bash
cd /opt/crypto-bot
pm2 start ecosystem.config.js
pm2 save
pm2 startup
pm2 logs krypto-bot
```

## Execução com systemd

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin cryptobot
sudo chown -R cryptobot:cryptobot /opt/crypto-bot
sudo chmod 700 /opt/crypto-bot/data /opt/crypto-bot/logs
sudo chmod 600 /opt/crypto-bot/.env
sudo cp deploy/crypto-bot.service /etc/systemd/system/crypto-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-bot
sudo systemctl status crypto-bot
```

## Logs e Telegram

O log vai para console e `logs/bot.log`, com rotação. Alertas de erro e crítico dos módulos `bot*` são enviados ao Telegram quando habilitado, com deduplicação e limite de frequência.

## Testes e Qualidade

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

## Licença e Responsabilidade

Use por sua conta e risco. O autor não se responsabiliza por perdas financeiras, falhas de execução, indisponibilidade da exchange, mudanças de API ou uso indevido das chaves.

---

**Instruções:** Sempre que modificar este projeto, atualize este arquivo `PROJECT_OVERVIEW.md` para refletir as mudanças. Isso permite que qualquer IA saiba em que projeto estamos e o que foi alterado.