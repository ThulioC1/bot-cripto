# KryptoBot

Bot de trading automatizado para **Binance Spot**, escrito em Python com `ccxt`. Opera apenas comprado (long-only), sem margem, sem futuros e sem alavancagem.

O objetivo do projeto é executar uma estratégia conservadora com gestão de risco rígida:

- entradas por confluência de EMA, RSI, MACD, Bollinger, ATR e volume;
- stop-loss obrigatório na exchange logo após a compra;
- take-profit monitorado pelo bot e executado a mercado;
- trailing stop opcional;
- circuit breaker por drawdown;
- limite de risco por operação, de posição e de exposição total;
- reconciliação de posições e stops após reinício;
- notificações opcionais por Telegram;
- supervisão por PM2 ou systemd.

> **Aviso:** este software pode perder dinheiro. Não existe garantia de lucro. Use primeiro testnet e depois capital que você aceita perder por completo. Mantenha saques desabilitados na API key e use uma subconta dedicada.

## Requisitos

- Python 3.10 ou superior (testado com 3.13 no desenvolvimento).
- Conta Binance Spot.
- Para operar de verdade: API key com leitura e Spot Trading habilitados, saques desabilitados e, preferencialmente, IP whitelist.
- Linux é o ambiente recomendado para produção. O bot também executa no Windows para desenvolvimento.

## Instalação

```bash
cd /opt/crypto-bot
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

No Windows/PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\pip.exe install -r requirements.txt
Copy-Item .env.example .env
```

Edite o `.env`. O arquivo `.env` do diretório do projeto é carregado automaticamente, mas variáveis já presentes no ambiente têm precedência. Nunca faça commit do `.env`.

## Configuração segura

O modo padrão é `DRY_RUN=true`. Nesse modo o bot baixa dados públicos, calcula sinais e mostra o dimensionamento, mas **não envia ordens**. Ele também não mantém uma carteira simulada entre ciclos; é um modo de validação de sinais, não um backtest.

Ordem recomendada de implantação:

1. Rode `--check` em dry-run.
2. Configure uma API key de **testnet** e rode com `SANDBOX=true`, `DRY_RUN=false`.
3. Observe o comportamento por tempo suficiente.
4. Só então use uma subconta real com pouco capital, `SANDBOX=false`, `DRY_RUN=false` e `CONFIRM_LIVE_TRADING=YES`.

Principais variáveis:

| Variável | Padrão | Observação |
|---|---:|---|
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
| `MIN_QUOTE_RESERVE_PCT` | `20` | Caixa mínimo que não pode ser gasto |
| `MIN_ORDER_QUOTE` | `10` | Notional mínimo interno por ordem |
| `TP_RR` | `2` | Relação risco-retorno mínima de 1.5 |
| `CIRCUIT_BREAKER_FLATTEN` | `true` | Liquida posições quando o breaker dispara |
| `ENFORCE_API_PERMISSIONS` | `true` | Verificação ativa de permissões na Binance |
| `API_PERMISSION_CHECK_STRICT` | `true` | Falha fechada se a verificação não funcionar |
| `TELEGRAM_ENABLED` | `false` | Exige token e chat id |

Os limites de risco acima são validados no código e não podem ser afrouxados apenas pelo `.env` além dos limites duros.

Segredos podem vir de arquivos em vez do `.env`: `EXCHANGE_API_KEY_FILE`, `EXCHANGE_API_SECRET_FILE` e `TELEGRAM_BOT_TOKEN_FILE`. Não defina a variável direta e a versão `_FILE` ao mesmo tempo.

### API key

Na Binance:

1. Crie uma subconta dedicada ao bot.
2. Gere uma API key apenas para ela.
3. Habilite leitura e Spot Trading.
4. **Desabilite saques.**
5. Restrinja o IP do servidor, se possível.
6. Não reutilize chaves de outras aplicações.

Em modo live, a Binance é consultada via `sapiGetAccountApiRestrictions`. O bot recusa a inicialização se detectar saques, margem, futuros ou opções habilitados. Se a consulta falhar, o comportamento padrão é falhar fechado. A verificação é ignorada em testnet.

## Uso

Validação sem enviar ordens:

```bash
.venv/bin/python main.py --check
```

Em dry-run e testnet, o `--check` não consulta permissões da API key; valida apenas conexão pública, pares, candles e indicadores. Em live com `ENFORCE_API_PERMISSIONS=true`, a checagem de permissões também é executada.

Dry-run com o loop completo:

```bash
.venv/bin/python main.py
```

Forçar dry-run por linha de comando:

```bash
.venv/bin/python main.py --dry-run
```

Resetar o circuit breaker (faça com o bot parado):

```bash
.venv/bin/python main.py --reset-halt
```

O reset atua no arquivo de estado do modo atual (`DRY_RUN`/`SANDBOX`). Para resetar o live, use a mesma configuração de produção e `DRY_RUN=false`.

Antes de reiniciar após um halt, confira na exchange:

- saldo em caixa e moedas;
- posições realmente abertas;
- ordens de stop abertas;
- ordens manuais que possam bloquear saldo.

## Estratégia

O sinal de compra só é considerado quando os filtros obrigatórios passam:

- ATR% dentro da faixa configurada;
- volume de 24h suficiente;
- RSI não sobrecomprado;
- último candle sem movimento excessivo;
- tendência de alta (EMA rápida acima da lenta, EMA lenta inclinada para cima e preço acima dela);
- preço não excessivamente esticado acima da EMA rápida.

Depois disso, o bot calcula um score de 0 a 7 com:

- separação das EMAs;
- MACD acima do sinal;
- histograma MACD positivo e acelerando;
- faixa saudável de RSI;
- preço entre a banda média e a superior;
- volume acima da média;
- rompimento ou proximidade da máxima recente.

O mínimo padrão é 4/7. O stop é calculado por ATR e estrutura, limitado entre 1,5% e 4%. O take-profit usa `TP_RR`.

## Como as ordens são protegidas

1. A compra é executada a mercado, com notional aprovado pelo risco.
2. Imediatamente após a confirmação, o bot registra a posição e envia um `STOP_LOSS` de venda para a exchange.
3. Se o stop não puder ser colocado, o bot tenta vender a posição imediatamente. Se também não conseguir, encerra com código fatal para o supervisor reiniciar e reconciliar.
4. O take-profit é local: quando o preço atinge o alvo, o bot cancela o stop, vende a mercado e registra o resultado.
5. O trailing stop cancela o stop antigo e coloca um novo mais alto. Existe uma janela curta sem stop durante essa troca; se a troca falhar, o bot tenta restaurar o stop anterior ou liquidar a posição.
6. Em reinicialização, o bot reconcilia o estado com saldo, ordens abertas e stops. Stops do bot usam `clientOrderId` com prefixo `KRB`.

A Binance Spot não oferece OCO unificado no `ccxt`; ordens de stop também bloqueiam a moeda base. Por isso o take-profit é local e não existe stop e take-profit simultâneos na exchange.

Limitações importantes:

- o stop pode executar abaixo do gatilho em quedas rápidas ou mercados ilíquidos;
- se o bot estiver offline, o stop na exchange continua protegendo, mas o take-profit local não será executado;
- durante manutenção da exchange ou falha de rede, ordens podem ficar em estado incerto;
- não há garantia de execução no preço exato;
- não use saldo que precise de liquidez imediata.

## Circuit breaker

O drawdown é medido contra o pico de equity das últimas 24 horas, não contra o fechamento do dia calendário. Ao atingir o limite:

- o estado é marcado como `halted` e persistido;
- novas entradas são bloqueadas;
- com `CIRCUIT_BREAKER_FLATTEN=true`, o bot cancela ordens do bot e tenta liquidar as posições;
- o processo termina com código 78 e não reinicia sozinho sob PM2/systemd;
- o halt só sai com `--reset-halt`.

Se a liquidação falhar, o processo termina com código 1 para o supervisor reiniciar. A reconciliação restaura stops antes de respeitar o halt.

## Arquivos e estado

- `data/state.dryrun.json` — dry-run;
- `data/state.testnet.json` — testnet;
- `data/state.json` — live (ou o nome definido em `STATE_FILE`);
- `*.json.bak` — backup automático;
- `*.json.corrupt-*` — estado corrompido preservado para análise;
- `logs/bot.log` — log rotativo;
- `.env` — segredos e configuração local.

O estado é gravado de forma atômica. Se o arquivo principal sumir, o backup é restaurado. Ainda assim, faça backups próprios e nunca apague `data/` com posições abertas.

Em live, se houver saldo em moeda base de um par configurado sem a posição correspondente no estado, o bot recusa iniciar com código 78. Isso evita operar sobre uma posição órfã; regularize a conta ou restaure o estado antes de continuar.

## Execução com PM2

O arquivo `ecosystem.config.js` usa `.venv/bin/python`, encerra em código 0/78 e reinicia em falhas transitórias:

```bash
cd /opt/crypto-bot
pm2 start ecosystem.config.js
pm2 save
pm2 startup
pm2 logs krypto-bot
pm2 status
```

Não rode PM2 e systemd ao mesmo tempo. O PM2 é útil quando você já usa PM2 para outros serviços; systemd é a opção mais simples em um servidor dedicado.

## Execução com systemd

O arquivo `deploy/crypto-bot.service` assume:

- projeto em `/opt/crypto-bot`;
- usuário e grupo `cryptobot`;
- `.env` em `/opt/crypto-bot/.env`;
- diretórios `data/` e `logs/` graváveis pelo usuário.

Instalação:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin cryptobot
sudo chown -R cryptobot:cryptobot /opt/crypto-bot
sudo chmod 700 /opt/crypto-bot/data /opt/crypto-bot/logs
sudo chmod 600 /opt/crypto-bot/.env
sudo cp deploy/crypto-bot.service /etc/systemd/system/crypto-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-bot
sudo systemctl status crypto-bot
sudo journalctl -u crypto-bot -f
```

O serviço reinicia em falha, mas considera o código 78 uma parada intencional. Para parar:

```bash
sudo systemctl stop crypto-bot
```

## Códigos de saída

| Código | Significado | Supervisor |
|---:|---|---|
| `0` | parada normal | não reinicia |
| `78` | configuração, autenticação, segurança, persistência ou circuit breaker | não reinicia |
| `1` | falha transitória ou estado de execução inseguro | reinicia |

## Logs e Telegram

O log vai para console e `logs/bot.log`, com rotação. Alertas de erro e crítico dos módulos `bot*` são enviados ao Telegram quando habilitado, com deduplicação e limite de frequência. Entradas e saídas também geram mensagens informativas.

O token e o chat id nunca devem aparecer no repositório. O bot não registra o token.

## Testes e qualidade

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

Os testes cobrem indicadores, sizing, limites, circuit breaker, persistência/backup, parsing de ordens e validações de configuração. Nenhum teste usa ordens reais.

## Solução de problemas

- **`DRY_RUN=false` recusado:** faltam chaves ou `CONFIRM_LIVE_TRADING=YES`.
- **Erro de segurança:** a API key tem saque, margem, futuros ou opções habilitados; corrija na Binance.
- **Saldo base bloqueado:** pode haver stop aberto ou ordem manual. O bot cancela apenas ordens com prefixo `KRB` ou stops registrados no estado.
- **Estado corrompido:** verifique `data/*.corrupt-*`; o backup é restaurado automaticamente quando válido.
- **Halt persistente:** use `--reset-halt` com o bot parado e confira a exchange antes.
- **Sem sinal:** os filtros são conservadores; é normal ficar fora do mercado.
- **Telegram silencioso:** confira token, chat id, se o bot iniciou uma conversa com o bot do Telegram e os logs de HTTP.

## Estrutura

```text
main.py              orquestração, loop, reconciliação e proteção
config.py            configuração e validação de limites
exchange.py          integração ccxt/Binance, retries e permissões
strategy.py          indicadores e sinal
risk_manager.py      sizing, estado, circuit breaker e trailing
logger.py            logs rotativos e Telegram
tests/               testes unitários
deploy/              unidade systemd
ecosystem.config.js  configuração PM2
.env.example         modelo de configuração
```

## Licença e responsabilidade

Use por sua conta e risco. O autor não se responsabiliza por perdas financeiras, falhas de execução, indisponibilidade da exchange, mudanças de API ou uso indevido das chaves.
