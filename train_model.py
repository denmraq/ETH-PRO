import time
import requests
import pandas as pd
from catboost import CatBoostClassifier

OKX_URL = "https://www.okx.com"
INST_ID = "ETH-USDT-SWAP"
MODEL_PATH = "catboost_eth_model.cbm"

def okx_get(path, params=None):
    r = requests.get(
        f"{OKX_URL}{path}",
        params=params or {},
        timeout=30,
        headers={"User-Agent": "ETH-PRO/1.0"},
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != "0":
        raise RuntimeError(payload.get("msg") or f"OKX error code {payload.get('code')}")
    return payload.get("data", [])

def fetch_hourly_history(target=2160):
    """
    OKX history-candles: собираем до ~90 дней часовых данных.
    Это ~2160 наблюдений, достаточно для первой рабочей CatBoost-модели.
    """
    rows = []
    after = None

    while len(rows) < target:
        params = {
            "instId": INST_ID,
            "bar": "1H",
            "limit": "100",
        }
        if after is not None:
            params["after"] = str(after)

        batch = okx_get("/api/v5/market/history-candles", params)
        if not batch:
            break

        rows.extend(batch)
        oldest_ts = min(int(x[0]) for x in batch)

        if after is not None and oldest_ts >= after:
            break

        after = oldest_ts
        print(f"Candles: {len(rows)}")
        time.sleep(0.12)

        if len(batch) < 100:
            break

    cols = ["time","open","high","low","close","volume","vol_ccy","qav","confirm"]
    df = pd.DataFrame(rows, columns=cols)

    for col in ["time","open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = (
        df.drop_duplicates("time")
          .sort_values("time")
          .reset_index(drop=True)
    )
    return df

def fetch_funding_history(oldest_ts):
    rows = []
    after = None

    for _ in range(40):
        params = {
            "instId": INST_ID,
            "limit": "100",
        }
        if after is not None:
            params["after"] = str(after)

        batch = okx_get("/api/v5/public/funding-rate-history", params)
        if not batch:
            break

        rows.extend(batch)

        times = [int(x["fundingTime"]) for x in batch]
        oldest = min(times)

        print(f"Funding rows: {len(rows)}")

        if oldest <= oldest_ts:
            break
        if after is not None and oldest >= after:
            break

        after = oldest
        time.sleep(0.12)

        if len(batch) < 100:
            break

    if not rows:
        return pd.DataFrame(columns=["fundingTime","funding_rate"])

    f = pd.DataFrame(rows)
    f["fundingTime"] = pd.to_numeric(f["fundingTime"], errors="coerce")
    f["funding_rate"] = pd.to_numeric(f["fundingRate"], errors="coerce")
    return (
        f[["fundingTime","funding_rate"]]
        .drop_duplicates("fundingTime")
        .sort_values("fundingTime")
        .reset_index(drop=True)
    )

def train_model():
    df = fetch_hourly_history()
    if len(df) < 500:
        raise RuntimeError(f"Недостаточно исторических свечей OKX: {len(df)}")

    funding = fetch_funding_history(int(df["time"].iloc[0]))

    if funding.empty:
        df["funding_rate"] = 0.0
    else:
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

    # Gemini 12H target:
    # 1, если через 12 часов цена выше более чем на 1.5%.
    future_return = df["close"].shift(-12) / df["close"] - 1.0
    df["target"] = (future_return > 0.015).astype(int)

    # Последние 12 строк не имеют известного future target.
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
    train_model()
