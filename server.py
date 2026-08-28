import os
import threading
import requests
import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from catboost import CatBoostClassifier

app = FastAPI(title="Binary Crypto ML Radar Engine")

MODEL_PATH = "catboost_eth_model.cbm"
OKX_URL = "https://www.okx.com"
INST_ID = "ETH-USDT-SWAP"

ml_model = None
model_training = False
model_error = None

# -----------------------------
# SIGNAL STABILIZER
# -----------------------------
signal_state = {
    "1h": {
        "direction": None,
        "probability": 0.50,
        "confidence": 0.0,
        "pending": None,
        "pending_count": 0,
        "last_candle_ts": None,
    },
    "12h": {
        "direction": None,
        "probability": 0.50,
        "confidence": 0.0,
        "last_candle_ts": None,
    },
    "24h": {
        "direction": None,
        "probability": 0.50,
        "confidence": 0.0,
        "last_candle_ts": None,
    },
}

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
        raise RuntimeError(payload.get("msg") or f"OKX error {payload.get('code')}")
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

def fetch_market_data():
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

    cols = ["t","o","h","l","c","v","vol_ccy","qav","confirm"]
    df_15m = pd.DataFrame(candles_15m, columns=cols)
    df_1h = pd.DataFrame(candles_1h, columns=cols)

    for df in (df_15m, df_1h):
        for col in ["t","o","h","l","c","v","qav","confirm"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.sort_values("t", inplace=True)
        df.reset_index(drop=True, inplace=True)

    funding_rate = 0.0
    if funding_data:
        funding_rate = float(funding_data[0].get("fundingRate") or 0.0)

    return df_15m, df_1h, funding_rate, trades

def format_binary(direction, raw_p):
    win_prob = raw_p if direction == "LONG" else (1.0 - raw_p)
    confidence = abs(raw_p - 0.50) * 200.0
    return {
        "direction": direction,
        "probability": round(win_prob * 100, 1),
        "confidence": round(confidence, 1),
    }

def stabilize_1h(raw_p, closed_15m_ts):
    """
    1H:
    - пересмотр только после НОВОЙ закрытой 15m свечи;
    - внутри этих 15 минут направление и цифры не меняются;
    - для переворота нужно 2 подтверждения на двух разных 15m свечах;
    - пороги: >=55% LONG, <=45% SHORT.
    """
    s = signal_state["1h"]

    if s["last_candle_ts"] == closed_15m_ts and s["direction"] is not None:
        return

    if s["direction"] is None:
        s["direction"] = "LONG" if raw_p > 0.50 else "SHORT"
    else:
        if raw_p >= 0.55:
            candidate = "LONG"
        elif raw_p <= 0.45:
            candidate = "SHORT"
        else:
            candidate = s["direction"]

        if candidate == s["direction"]:
            s["pending"] = None
            s["pending_count"] = 0
        else:
            if s["pending"] == candidate:
                s["pending_count"] += 1
            else:
                s["pending"] = candidate
                s["pending_count"] = 1

            if s["pending_count"] >= 2:
                s["direction"] = candidate
                s["pending"] = None
                s["pending_count"] = 0

    s["probability"] = raw_p
    s["confidence"] = abs(raw_p - 0.50) * 200.0
    s["last_candle_ts"] = closed_15m_ts

def stabilize_slow(key, raw_p, closed_1h_ts, long_thr, short_thr):
    """
    12H/24H:
    - пересчет только после новой закрытой 1H свечи;
    - между порогами сохраняется предыдущая сторона.
    """
    s = signal_state[key]

    if s["last_candle_ts"] == closed_1h_ts and s["direction"] is not None:
        return

    if s["direction"] is None:
        direction = "LONG" if raw_p > 0.50 else "SHORT"
    else:
        direction = s["direction"]
        if raw_p >= long_thr:
            direction = "LONG"
        elif raw_p <= short_thr:
            direction = "SHORT"

    s["direction"] = direction
    s["probability"] = raw_p
    s["confidence"] = abs(raw_p - 0.50) * 200.0
    s["last_candle_ts"] = closed_1h_ts

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
        df_15m, df_1h, funding_rate, trades = fetch_market_data()

        closed_15m = df_15m[df_15m["confirm"] == 1].copy()
        closed_1h = df_1h[df_1h["confirm"] == 1].copy()

        if len(closed_15m) < 4:
            raise RuntimeError("Недостаточно закрытых 15m свечей")
        if len(closed_1h) < 73:
            raise RuntimeError("Недостаточно закрытых 1H свечей")

        latest_closed_15m_ts = int(closed_15m["t"].iloc[-1])
        latest_closed_1h_ts = int(closed_1h["t"].iloc[-1])

        # -----------------------------
        # 1H — Microstructure Orderflow
        # -----------------------------
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
        raw_1h = buy_notional / total_notional if total_notional > 0 else 0.50
        raw_1h = float(np.clip(raw_1h, 0.01, 0.99))
        stabilize_1h(raw_1h, latest_closed_15m_ts)

        # -----------------------------
        # 12H — CatBoost ML
        # -----------------------------
        if ml_model is not None:
            ml_df = closed_1h.copy()
            ml_df["returns"] = ml_df["c"].pct_change()
            ml_df["volatility"] = ml_df["returns"].rolling(12).std()
            ml_df["volume_change"] = ml_df["v"].pct_change()
            ml_df["funding_rate"] = funding_rate

            features = ml_df[
                ["returns", "volatility", "volume_change", "funding_rate"]
            ].iloc[[-1]]

            raw_12h = float(ml_model.predict_proba(features)[0][1])
        else:
            raw_12h = 0.51 if closed_1h["c"].iloc[-1] > closed_1h["c"].iloc[-2] else 0.49

        stabilize_slow(
            "12h",
            raw_12h,
            latest_closed_1h_ts,
            long_thr=0.57,
            short_thr=0.43,
        )

        # -----------------------------
        # 24H — Trend & Funding Risk
        # -----------------------------
        ema_24 = closed_1h["c"].ewm(span=24).mean().iloc[-1]
        ema_72 = closed_1h["c"].ewm(span=72).mean().iloc[-1]

        trend = 1 if ema_24 > ema_72 else -1
        funding_bias = (
            -0.10 if funding_rate > 0.0003
            else (0.10 if funding_rate < -0.0001 else 0.0)
        )

        raw_24h = float(
            np.clip(0.50 + (trend * 0.15) + funding_bias, 0.01, 0.99)
        )

        stabilize_slow(
            "24h",
            raw_24h,
            latest_closed_1h_ts,
            long_thr=0.58,
            short_thr=0.42,
        )

        s1 = signal_state["1h"]
        s12 = signal_state["12h"]
        s24 = signal_state["24h"]

        return {
            "status": "ok",
            "symbol": "ETH/USDT",
            "market_source": "OKX ETH-USDT-SWAP",
            "model_loaded": ml_model is not None,
            "model_training": model_training,
            "stabilizer": "active",
            "horizon_1h": format_binary(s1["direction"], s1["probability"]),
            "horizon_12h": format_binary(s12["direction"], s12["probability"]),
            "horizon_24h": format_binary(s24["direction"], s24["probability"]),
            "meta": {
                "latest_closed_15m_ts": latest_closed_15m_ts,
                "latest_closed_1h_ts": latest_closed_1h_ts,
                "h1_pending_flip": s1["pending"],
                "h1_pending_count": s1["pending_count"],
            },
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
def read_root():
    return FileResponse("static/index.html")
