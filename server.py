import numpy as np
import pandas as pd
import requests
from catboost import CatBoostClassifier
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Dict, Union

app = FastAPI(title="Crypto Multi-Horizon Predictor Engine")

SYMBOL = "ETH/USDT"
BINANCE_FUTURES_URL = "https://fapi.binance.com"

# Реальная обученная CatBoost-модель для 12ч
model_12h = CatBoostClassifier()
model_12h.load_model("catboost_eth_12h.cbm")


class PredictionResponse(BaseModel):
    symbol: str
    horizon_1h: Dict[str, Union[float, str]]
    horizon_12h: Dict[str, Union[float, str]]
    horizon_24h: Dict[str, Union[float, str]]


def fetch_market_data():
    """Сбор рыночных данных с фьючерсного рынка Binance"""
    candles_15m = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/klines",
        params={"symbol": "ETHUSDT", "interval": "15m", "limit": 100},
    ).json()

    candles_1h = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/klines",
        params={"symbol": "ETHUSDT", "interval": "1h", "limit": 100},
    ).json()

    funding_info = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex",
        params={"symbol": "ETHUSDT"},
    ).json()

    open_interest = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/openInterest",
        params={"symbol": "ETHUSDT"},
    ).json()

    df_15m = pd.DataFrame(
        candles_15m,
        columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades", "taker_base_vol",
            "taker_quote_vol", "ignore",
        ],
    ).astype(float)

    df_1h = pd.DataFrame(
        candles_1h,
        columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades", "taker_base_vol",
            "taker_quote_vol", "ignore",
        ],
    ).astype(float)

    return {
        "df_15m": df_15m,
        "df_1h": df_1h,
        "funding_rate": float(funding_info.get("lastFundingRate", 0)),
        "open_interest": float(open_interest.get("openInterest", 0)),
    }


def predict_1h_horizon(data: dict) -> Dict[str, Union[float, str]]:
    """Прогноз на 1 час: Taker Buy/Sell Volume Imbalance"""
    df = data["df_15m"].tail(4)

    taker_buy_vol = df["taker_quote_vol"].sum()
    total_vol = df["qav"].sum()
    buy_ratio = taker_buy_vol / total_vol if total_vol > 0 else 0.5

    probability_up = float(np.clip(buy_ratio * 1.1, 0.0, 1.0))

    direction = (
        "LONG" if probability_up > 0.58
        else ("SHORT" if probability_up < 0.42 else "NEUTRAL")
    )
    confidence = abs(probability_up - 0.5) * 2

    return {
        "direction": direction,
        "confidence_score": round(confidence, 2),
        "prob_up": round(probability_up, 2),
    }


def get_live_features_12h(data: dict):
    """Свежие фичи в точности под формат обучения 12ч CatBoost"""
    df = data["df_1h"].copy()

    df["returns"] = df["close"].pct_change()
    df["volatility"] = df["returns"].rolling(12).std()
    df["volume_change"] = df["volume"].pct_change()
    df["funding_rate"] = data["funding_rate"]

    return df[["returns", "volatility", "volume_change", "funding_rate"]].iloc[[-1]]


def predict_12h_horizon(data: dict) -> Dict[str, Union[float, str]]:
    """Прогноз на 12 часов: реальная CatBoost-модель"""
    features = get_live_features_12h(data)

    probabilities = model_12h.predict_proba(features)[0]
    prob_up = float(probabilities[1])

    if prob_up > 0.65:
        direction = "LONG"
    elif prob_up < 0.35:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "confidence_score": round(abs(prob_up - 0.5) * 2, 4),
        "prob_up": round(prob_up, 4),
    }


def predict_24h_horizon(data: dict) -> Dict[str, Union[float, str]]:
    """Прогноз на 24 часа: среднесрочный тренд и деривативы"""
    df = data["df_1h"]

    ema_fast = df["close"].ewm(span=24).mean().iloc[-1]
    ema_slow = df["close"].ewm(span=72).mean().iloc[-1]

    trend_signal = 1.0 if ema_fast > ema_slow else -1.0
    prob_up = 0.5 + (trend_signal * 0.2)

    direction = (
        "LONG" if prob_up > 0.55
        else ("SHORT" if prob_up < 0.45 else "NEUTRAL")
    )

    return {
        "direction": direction,
        "confidence_score": round(abs(prob_up - 0.5) * 2, 2),
        "prob_up": round(prob_up, 2),
    }


@app.get("/api/v1/predict", response_model=PredictionResponse)
def get_predictions():
    data = fetch_market_data()

    return {
        "symbol": SYMBOL,
        "horizon_1h": predict_1h_horizon(data),
        "horizon_12h": predict_12h_horizon(data),
        "horizon_24h": predict_24h_horizon(data),
    }


@app.get("/predict/12h")
def predict_ml_12h():
    data = fetch_market_data()
    result = predict_12h_horizon(data)

    return {
        "symbol": "ETHUSDT",
        "horizon": "12h",
        "signal": result["direction"],
        "probability_up": result["prob_up"],
        "confidence": result["confidence_score"],
    }


@app.get("/api/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
