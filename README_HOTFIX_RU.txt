HOTFIX V1.25 — ошибка 500 на главной странице

Причина: server.py пытался отдать /app/static/index.html, а в контейнере Render этого файла не оказалось.

Что изменено:
1) server.py ищет интерфейс сначала static/index.html, затем запасной root index.html.
2) В архиве index.html положен в ОБА места: /index.html и /static/index.html.
3) Удалена регистрация service-worker.js. Его возвращать не нужно.
4) /api/health и логика прогнозатора не менялись.

Что загрузить в GitHub:
- server.py — заменить существующий
- index.html — добавить в корень репозитория
- папку static — проверить, что внутри есть index.html + manifest + icons

После commit Render с Auto-Deploy должен пересобрать сервис.
