# ETH Order Flow Radar V1.0

Отдельный live-радар ETHUSDT perpetual по принципу Bookmap/Exocharts: не торговый бот, ордера не отправляет.

## Что анализирует

- Bybit public WebSocket `orderbook.200.ETHUSDT` — живой L200 стакан.
- `publicTrade.ETHUSDT` — реальные исполненные сделки, Delta/CVD и агрессивный BUY/SELL flow.
- `allLiquidation.ETHUSDT` — все ликвидации Bybit.
- Ticker + REST — Mark Price, Funding, Open Interest.
- Order-book imbalance.
- Seller/Buyer absorption.
- Buy/Sell exhaustion.
- Микроструктуру последних сделок.
- Всегда выдаёт только LONG или SHORT. Слабость отражается в `СИЛА ПРЕИМУЩЕСТВА`, WAIT отсутствует.

## Market Target

Тейк НЕ считается как процент от входа. Радар ищет реальный кластер противоположной ликвидности в L200 стакане с учётом аномального размера и достижимости. Если подходящего кластера временно нет, используется реально проторгованный локальный структурный экстремум. Цель пересчитывается вместе со стаканом.

## GitHub → Render

1. Создать новый GitHub repository.
2. Загрузить все файлы из папки проекта в корень repository.
3. В Render: New → Web Service → подключить GitHub repository.
4. Render увидит `render.yaml`/`Dockerfile` и запустит сервис.
5. Открыть выданный HTTPS URL.

## GitHub → AlexHost / любой VPS с Docker

```bash
git clone https://github.com/USERNAME/REPO.git
cd REPO
docker build -t eth-orderflow-radar .
docker run -d --restart unless-stopped -p 8000:8000 --name eth-radar eth-orderflow-radar
```

Далее домен можно направить через nginx/Caddy на `127.0.0.1:8000`.

## Проверка

- `/` — интерфейс.
- `/api/radar` — JSON текущего решения.
- `/api/health` — состояние потоков.

## Важно по трактовке

`СИЛА ПРЕИМУЩЕСТВА` — это не заявленный win rate. Это нормированный текущий перевес микроcтруктуры. После запуска радару нужно несколько минут, чтобы набрать полноценное окно trade-flow.
