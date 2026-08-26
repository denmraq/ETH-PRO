# ETH Entry Radar PRO V1.1

Separate PRO branch/application based on ETH Entry Radar iOS V0.3.7 Core.

## Included
- ETHUSDT perpetual: 4H / 1H / 15m / 5m closed-candle analysis.
- LONG / SHORT scoring, entry zone, STOP, TP1/TP2/TP3.
- Time Engine with historical analogs and TP/SL/neither timing statistics.
- Fixed multi-window Flow: real accumulated 5m + 15m aggressive buy/sell delta and CVD.
- OI and funding.
- News / Macro Engine from official Fed, BLS and SEC RSS feeds.
- News impact classification (LOW/MEDIUM/HIGH), semantic bias and observed ETH/BTC reaction after detection.
- Background 60-second monitor on the server.
- Optional iOS Web Push for signal changes and newly detected HIGH-impact news.
- SQLite state for news, push subscriptions and monitor state. Use a persistent disk in production.

## Important limits
The News Engine does **not** invent forecast/actual economic values. RSS can arrive with delay. `ETH NEWS BIAS` combines limited headline semantics with the observed ETH/BTC reaction after the server detects the event. It is not a guaranteed causal estimate.

For 24/7 monitoring the hosting service itself must remain awake. The included Render blueprint uses a paid Starter web service and persistent disk because sleeping/ephemeral hosting cannot guarantee continuous monitoring.

## Local launch
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```
Open http://127.0.0.1:8000

## Push notifications
1. Run `python generate_vapid_keys.py`.
2. Copy `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` into hosting environment variables.
3. Set `VAPID_SUBJECT` to a mailto address you control.
4. Deploy over HTTPS.
5. Install the PWA to the iPhone Home Screen.
6. Open it from the Home Screen and tap **Включить Push**.

On iOS, Web Push requires a Home Screen web app and user permission.


## Priority Push V1.1
Push-сигнал отправляется только при преимуществе одной стороны минимум на 5 баллов. Например LONG 30 / SHORT 35 = подтвержденный SHORT priority. Если разница меньше 5, уведомления нет. При смене подтвержденного приоритета LONG↔SHORT push показывает, сколько времени прошло с предыдущего подтвержденного приоритета.
