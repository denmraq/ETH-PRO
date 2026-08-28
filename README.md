# ETH-PRO

Полная версия для Render.

Причина перехода с Binance на OKX:
Render получает HTTP 451 от Binance Futures API. Это блокировка по локации/IP дата-центра, а не ошибка Python.

Логика прогноза сохранена:
- 1H: microstructure / taker buy-sell imbalance
- 12H: CatBoost, признаки returns + volatility + volume_change + funding_rate
- 24H: EMA trend + funding bias

Модель CatBoost обучается в фоне после запуска сервера, поэтому health check Render не блокируется.
