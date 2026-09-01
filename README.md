# ETH Radar Medium V2

GitHub Pages / PWA версия ETH/USDT радара.

## Что изменено
- Только LONG или SHORT — WAIT отсутствует.
- Основной горизонт: 6–36 часов.
- LONG FORCE / SHORT FORCE: futures flow, acceleration, price response, absorption, OI, liquidity, trend, funding.
- LOCAL TARGET — ближайшая промежуточная зона.
- MAIN TARGET — среднесрочная цель, выбирается из кластеров 1H/4H swing-уровней, high/low 24h и 3d и projected move.
- EXTENDED — продолжение импульса.
- INVALIDATION — структурная отмена сценария.
- Entry zone — предпочтительная зона входа; она не меняет направление LONG/SHORT.

## Установка
Распакуй содержимое архива в корень GitHub repository и включи GitHub Pages.

Важно: FORCE — сила стороны внутри модели, а не статистически доказанная вероятность достижения цели.

## Render Docker deployment
This package includes a Dockerfile for Render services configured with Runtime = Docker.
No custom Docker command is required. The container starts `node server.js` and listens on `process.env.PORT` (default 10000).
Health check: `/health`.
