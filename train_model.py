import time
import requests
import pandas as pd
from catboost import CatBoostClassifier

BINANCE_URL = "https://fapi.binance.com"
SYMBOL = "ETHUSDT"
MODEL_PATH = "catboost_eth_model.cbm"

def get(path, params):
    r = requests.get(f"{BINANCE_URL}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_1h_history(total=17520):
    rows = []
    end_time = None

    while len(rows) < total:
        limit = min(1500, total - len(rows))
        params = {"symbol": SYMBOL, "interval": "1h", "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time

        batch = get("/fapi/v1/klines", params)
        if not batch:
            break

        rows = batch + rows
        end_time = int(batch[0][0]) - 1
        print(f"Candles: {len(rows)}")
        time.sleep(0.10)

        if len(batch) < limit:
            break

    cols = [
        "time","open","high","low","close","volume",
        "close_time","qav","num_trades",
        "taker_base_vol","taker_quote_vol","ignore"
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["time","open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.drop_duplicates("time").sort_values("time").reset_index(drop=True)

def fetch_funding(start_time, end_time):
    rows = []
    cursor = int(start_time)

    while cursor < end_time:
        batch = get("/fapi/v1/fundingRate", {
            "symbol": SYMBOL,
            "startTime": cursor,
            "endTime": int(end_time),
            "limit": 1000,
        })
        if not batch:
            break

        rows.extend(batch)
        last = int(batch[-1]["fundingTime"])
        if last <= cursor:
            break
        cursor = last + 1
        print(f"Funding rows: {len(rows)}")
        time.sleep(0.10)

        if len(batch) < 1000:
            break

    f = pd.DataFrame(rows)
    if f.empty:
        return pd.DataFrame(columns=["fundingTime","funding_rate"])

    f["fundingTime"] = pd.to_numeric(f["fundingTime"], errors="coerce")
    f["funding_rate"] = pd.to_numeric(f["fundingRate"], errors="coerce")
    return (
        f[["fundingTime","funding_rate"]]
        .drop_duplicates("fundingTime")
        .sort_values("fundingTime")
    )

def main():
    df = fetch_1h_history()
    funding = fetch_funding(df["time"].iloc[0], df["time"].iloc[-1])

    df = pd.merge_asof(
        df.sort_values("time"),
        funding.sort_values("fundingTime"),
        left_on="time",
        right_on="fundingTime",
        direction="backward",
    )
    df["funding_rate"] = df["funding_rate"].fillna(0.0)

    # Gemini feature logic
    df["returns"] = df["close"].pct_change()
    df["volatility"] = df["returns"].rolling(12).std()
    df["volume_change"] = df["volume"].pct_change()

    # Gemini 12h target: > +1.5% after 12 hours
    future_return = df["close"].shift(-12) / df["close"] - 1
    df["target"] = (future_return > 0.015).astype(int)

    df = df.iloc[:-12].dropna().reset_index(drop=True)

    features = ["returns","volatility","volume_change","funding_rate"]
    X = df[features]
    y = df["target"]

    if y.nunique() < 2:
        raise RuntimeError("Training target has only one class")

    print(f"Training rows: {len(df)}")
    print(f"Positive class: {y.mean():.4f}")

    model = CatBoostClassifier(
        iterations=500,
        depth=6,
        learning_rate=0.03,
        loss_function="Logloss",
        random_seed=42,
        verbose=100,
    )
    model.fit(X, y)
    model.save_model(MODEL_PATH)
    print(f"Saved: {MODEL_PATH}")

if __name__ == "__main__":
    main()
