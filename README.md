# ETH-PRO

Полный минимальный комплект для Render.

Render сейчас настроен на Docker, поэтому Dockerfile ОБЯЗАТЕЛЕН.

Структура:
- Dockerfile
- .dockerignore
- render.yaml
- Procfile
- requirements.txt
- server.py
- train_model.py
- start.sh
- static/index.html

При первом запуске start.sh пытается обучить CatBoost-модель.
Если обучение не удаётся, сервер всё равно запускается, а 12H временно показывает fallback 50%.
