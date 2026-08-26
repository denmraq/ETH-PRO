# ETH Entry Radar — iOS Edition V0.3.7.2

## Что внутри
- `radar.py` — аналитический движок
- `server.py` — FastAPI сервер
- `static/index.html` — интерфейс iPhone
- `static/service-worker.js` — PWA service worker
- `static/manifest.webmanifest` — PWA manifest
- `static/icon-192.png`, `icon-512.png`, `apple-touch-icon.png` — иконки
- `requirements.txt`, `render.yaml`, `Dockerfile`, `Procfile` — deploy
- `test_radar.py` — базовые тесты
- локальные start-скрипты для Windows/macOS/Linux

## Сигнал
Итог всегда только `LONG` или `SHORT`.
Для выбранной стороны Radar показывает диапазон входа, Stop и TP. Сила преимущества и качество данных отображаются отдельно и не превращают результат в WAIT/NEUTRAL.

## Render
Region: Frankfurt (EU Central)
Build: `pip install -r requirements.txt`
Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`

## Установка на iPhone
1. Дождаться успешного deploy.
2. Открыть HTTPS URL в Safari.
3. Поделиться → На экран «Домой».
4. Нажать «Добавить».
