import os
import threading
import requests
import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from catboost import CatBoostClassifier

app = FastAPI(title="Crypto ML Radar Engine")

MODEL_PATH = "catboost_eth_model.cbm"
OKX_URL = "https://www.okx.com"
INST_ID = "ETH-USDT-SWAP"

ml_model = None
model_training = False
model_error = None

def okx_get(path, params=None):
    r = requests.get(
        f"{OKX_URL}{path}",
        params=params or {},
        timeout=20,
        headers={"User-Agent": "ETH-PRO/1.0"},
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != "0":
        raise RuntimeError(payload.get("msg") or f"OKX error code {payload.get('code')}")
    return payload.get("data", [])

def load_model():
    global ml_model, model_error
    if not os.path.exists(MODEL_PATH):
        return False
    try:
        model = CatBoostClassifier()
        model.load_model(MODEL_PATH)
        ml_model = model
        model_error = None
        print("CatBoost model loaded")
        return True
    except Exception as e:
        model_error = str(e)
        print(f"Model load error: {e}")
        return False

def train_in_background():
    global model_training, model_error
    if model_training or ml_model is not None:
        return
    model_training = True
    try:
        from train_model import train_model
        train_model()
        load_model()
    except Exception as e:
        model_error = str(e)
        print(f"Background training error: {e}")
    finally:
        model_training = False

load_model()

@app.on_event("startup")
def startup_event():
    if ml_model is None:
        threading.Thread(target=train_in_background, daemon=True).start()

def fetch_live_data():
    candles_15m = okx_get(
        "/api/v5/market/candles",
        {"instId": INST_ID, "bar": "15m", "limit": "50"},
    )
    candles_1h = okx_get(
        "/api/v5/market/candles",
        {"instId": INST_ID, "bar": "1H", "limit": "100"},
    )
    funding_data = okx_get(
        "/api/v5/public/funding-rate",
        {"instId": INST_ID},
    )
    trades = okx_get(
        "/api/v5/market/trades",
        {"instId": INST_ID, "limit": "500"},
    )

    # OKX candles:
    # [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
    cols = ["t","o","h","l","c","v","vol_ccy","qav","confirm"]

    df_15m = pd.DataFrame(candles_15m, columns=cols)
    df_1h = pd.DataFrame(candles_1h, columns=cols)

    for df in (df_15m, df_1h):
        for col in ["t","o","h","l","c","v","qav"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.sort_values("t", inplace=True)
        df.reset_index(drop=True, inplace=True)

    funding_rate = 0.0
    if funding_data:
        funding_rate = float(funding_data[0].get("fundingRate") or 0.0)

    return df_15m, df_1h, funding_rate, trades

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "model_loaded": ml_model is not None,
        "model_training": model_training,
        "model_error": model_error,
        "market_source": "OKX",
    }

@app.get("/api/v1/predict")
def predict_horizons():
    try:
        df_15m, df_1h, funding_rate, trades = fetch_live_data()

        # 1H — Gemini logic: microstructure / taker buy-sell imbalance.
        # На OKX считаем напрямую по последним сделкам.
        buy_notional = 0.0
        sell_notional = 0.0
        for tr in trades:
            px = float(tr.get("px") or 0.0)
            sz = float(tr.get("sz") or 0.0)
            notional = px * sz
            if tr.get("side") == "buy":
                buy_notional += notional
            elif tr.get("side") == "sell":
                sell_notional += notional

        total_notional = buy_notional + sell_notional
        taker_buy_ratio = buy_notional / total_notional if total_notional > 0 else 0.5
        prob_1h = float(np.clip(taker_buy_ratio * 1.05, 0.05, 0.95))

        # 12H — CatBoost по Gemini features.
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

        # 24H — Gemini logic: EMA trend + derivatives funding bias.
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

        def fmt(p):
            direction = "LONG" if p > 0.58 else ("SHORT" if p < 0.42 else "NEUTRAL")
            return {
                "direction": direction,
                "probability_up": round(p * 100, 1),
                "confidence": round(abs(p - 0.5) * 200, 1),
            }

        return {
            "status": "ok",
            "symbol": "ETH/USDT",
            "market_source": "OKX ETH-USDT-SWAP",
            "model_loaded": ml_model is not None,
            "model_training": model_training,
            "horizon_1h": fmt(prob_1h),
            "horizon_12h": fmt(prob_12h),
            "horizon_24h": fmt(prob_24h),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "model_loaded": ml_model is not None,
            "model_training": model_training,
        }

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")
