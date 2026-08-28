import os
import requests
import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from catboost import CatBoostClassifier

app = FastAPI(title="Crypto ML Radar Engine")

MODEL_PATH = "catboost_eth_model.cbm"
BINANCE_URL = "https://fapi.binance.com"

# Загрузка ML-модели при запуске
ml_model = None
if os.path.exists(MODEL_PATH):
    try:
        ml_model = CatBoostClassifier()
        ml_model.load_model(MODEL_PATH)
        print("CatBoost-модель успешно загружена")
    except Exception as e:
        print(f"Ошибка загрузки ML-модели: {e}")

def fetch_binance_data():
    """Сбор рыночных данных с фьючерсного рынка Binance"""
    c_15m = requests.get(
        f"{BINANCE_URL}/fapi/v1/klines",
        params={"symbol": "ETHUSDT", "interval": "15m", "limit": 50},
        timeout=15,
    )
    c_15m.raise_for_status()

    c_1h = requests.get(
        f"{BINANCE_URL}/fapi/v1/klines",
        params={"symbol": "ETHUSDT", "interval": "1h", "limit": 100},
        timeout=15,
    )
    c_1h.raise_for_status()

    funding = requests.get(
        f"{BINANCE_URL}/fapi/v1/premiumIndex",
        params={"symbol": "ETHUSDT"},
        timeout=15,
    )
    funding.raise_for_status()

    c_15m = c_15m.json()
    c_1h = c_1h.json()
    funding = funding.json()

    df_15m = pd.DataFrame(
        c_15m,
        columns=['t','o','h','l','c','v','ct','qav','n','tbv','tqv','i']
    ).astype(float)

    df_1h = pd.DataFrame(
        c_1h,
        columns=['t','o','h','l','c','v','ct','qav','n','tbv','tqv','i']
    ).astype(float)

    f_rate = float(funding.get("lastFundingRate", 0))
    return df_15m, df_1h, f_rate

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "model_loaded": ml_model is not None,
        "model_path": MODEL_PATH,
    }

@app.get("/api/v1/predict")
def predict_horizons():
    try:
        df_15m, df_1h, funding_rate = fetch_binance_data()

        # --- PROGNOSIS 1H ---
        # Микроструктура рынка и Taker Buy/Sell Volume Imbalance
        df_last_1h = df_15m.tail(4)
        total_vol = df_last_1h['qav'].sum()
        taker_buy_ratio = (
            df_last_1h['tqv'].sum() / total_vol
            if total_vol > 0 else 0.5
        )
        prob_1h = float(np.clip(taker_buy_ratio * 1.05, 0.05, 0.95))

        # --- PROGNOSIS 12H ---
        # Real ML Inference via CatBoost
        if ml_model is not None:
            df_1h['returns'] = df_1h['c'].pct_change()
            df_1h['volatility'] = df_1h['returns'].rolling(12).std()
            df_1h['volume_change'] = df_1h['v'].pct_change()
            df_1h['funding_rate'] = funding_rate

            features = df_1h[
                ['returns', 'volatility', 'volume_change', 'funding_rate']
            ].iloc[[-1]]

            prob_12h = float(ml_model.predict_proba(features)[0][1])
        else:
            prob_12h = 0.50

        # --- PROGNOSIS 24H ---
        # Тренд и Funding деривативов
        ema_24 = df_1h['c'].ewm(span=24).mean().iloc[-1]
        ema_72 = df_1h['c'].ewm(span=72).mean().iloc[-1]

        trend = 1 if ema_24 > ema_72 else -1
        funding_bias = (
            -0.15 if funding_rate > 0.0005
            else (0.15 if funding_rate < -0.0002 else 0)
        )

        prob_24h = float(
            np.clip(0.5 + (trend * 0.15) + funding_bias, 0.05, 0.95)
        )

        def format_res(p):
            direction = (
                "LONG" if p > 0.58
                else ("SHORT" if p < 0.42 else "NEUTRAL")
            )
            return {
                "direction": direction,
                "probability_up": round(p * 100, 1),
                "confidence": round(abs(p - 0.5) * 200, 1),
            }

        return {
            "status": "ok",
            "symbol": "ETH/USDT",
            "model_loaded": ml_model is not None,
            "horizon_1h": format_res(prob_1h),
            "horizon_12h": format_res(prob_12h),
            "horizon_24h": format_res(prob_24h),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "model_loaded": ml_model is not None,
        }

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_root():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    return {"message": "Crypto ML API is running. Direct to /api/v1/predict"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
