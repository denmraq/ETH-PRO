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

ml_model = None

def load_model():
    global ml_model
    if os.path.exists(MODEL_PATH):
        try:
            model = CatBoostClassifier()
            model.load_model(MODEL_PATH)
            ml_model = model
            print("CatBoost model loaded")
        except Exception as e:
            print(f"Model load error: {e}")

load_model()

def get_json(path, params):
    r = requests.get(f"{BINANCE_URL}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def fetch_binance_data():
    c_15m = get_json("/fapi/v1/klines", {
        "symbol": "ETHUSDT", "interval": "15m", "limit": 50
    })
    c_1h = get_json("/fapi/v1/klines", {
        "symbol": "ETHUSDT", "interval": "1h", "limit": 100
    })
    funding = get_json("/fapi/v1/premiumIndex", {"symbol": "ETHUSDT"})

    cols = ['t','o','h','l','c','v','ct','qav','n','tbv','tqv','i']
    df_15m = pd.DataFrame(c_15m, columns=cols).astype(float)
    df_1h = pd.DataFrame(c_1h, columns=cols).astype(float)
    f_rate = float(funding.get("lastFundingRate", 0))
    return df_15m, df_1h, f_rate

@app.get("/api/health")
def health():
    return {"status": "ok", "model_loaded": ml_model is not None}

@app.get("/api/v1/predict")
def predict_horizons():
    try:
        df_15m, df_1h, funding_rate = fetch_binance_data()

        # 1H — Gemini logic: taker buy/sell microstructure
        last_1h = df_15m.tail(4)
        total_quote = last_1h["qav"].sum()
        taker_buy_ratio = (
            last_1h["tqv"].sum() / total_quote
            if total_quote > 0 else 0.5
        )
        prob_1h = float(np.clip(taker_buy_ratio * 1.05, 0.05, 0.95))

        # 12H — CatBoost
        if ml_model is not None:
            df_1h["returns"] = df_1h["c"].pct_change()
            df_1h["volatility"] = df_1h["returns"].rolling(12).std()
            df_1h["volume_change"] = df_1h["v"].pct_change()
            df_1h["funding_rate"] = funding_rate

            features = df_1h[
                ["returns", "volatility", "volume_change", "funding_rate"]
            ].iloc[[-1]]

            prob_12h = float(ml_model.predict_proba(features)[0][1])
        else:
            prob_12h = 0.50

        # 24H — Gemini logic: EMA trend + derivatives funding bias
        ema_24 = df_1h["c"].ewm(span=24).mean().iloc[-1]
        ema_72 = df_1h["c"].ewm(span=72).mean().iloc[-1]
        trend = 1 if ema_24 > ema_72 else -1

        if funding_rate > 0.0005:
            funding_bias = -0.15
        elif funding_rate < -0.0002:
            funding_bias = 0.15
        else:
            funding_bias = 0.0

        prob_24h = float(
            np.clip(0.5 + trend * 0.15 + funding_bias, 0.05, 0.95)
        )

        def format_result(p):
            direction = "LONG" if p > 0.58 else ("SHORT" if p < 0.42 else "NEUTRAL")
            return {
                "direction": direction,
                "probability_up": round(p * 100, 1),
                "confidence": round(abs(p - 0.5) * 200, 1),
            }

        return {
            "status": "ok",
            "symbol": "ETH/USDT",
            "model_loaded": ml_model is not None,
            "horizon_1h": format_result(prob_1h),
            "horizon_12h": format_result(prob_12h),
            "horizon_24h": format_result(prob_24h),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "model_loaded": ml_model is not None,
        }

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")
