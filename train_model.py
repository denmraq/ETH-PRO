import time
import requests
import pandas as pd
from catboost import CatBoostClassifier

BINANCE_URL = "https://fapi.binance.com"
SYMBOL = "ETHUSDT"

# Gemini-логика:
# features = returns, volatility, volume_change, funding_rate
# target = 1, если через 12 часов ETH вырос более чем на 1.5%, иначе 0

def fetch_1h_history(total_candles=17520):
    """
    Загружает примерно 2 года часовых свечей.
    Binance отдаёт до 1500 свечей за запрос, поэтому идём назад пагинацией.
    """
    rows = []
    end_time = None

    while len(rows) < total_candles:
        limit = min(1500, total_candles - len(rows))
        params = {
            "symbol": SYMBOL,
            "interval": "1h",
            "limit": limit,
        }
        if end_time is not None:
            params["endTime"] = end_time

        r = requests.get(
            f"{BINANCE_URL}/fapi/v1/klines",
            params=params,
            timeout=20,
        )
        r.raise_for_status()
        batch = r.json()

        if not batch:
            break

        rows = batch + rows
        oldest_open_time = int(batch[0][0])
        end_time = oldest_open_time - 1

        print(f"Свечей загружено: {len(rows)}")
        time.sleep(0.15)

        if len(batch) < limit:
            break

    df = pd.DataFrame(
        rows,
        columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades",
            "taker_base_vol", "taker_quote_vol", "ignore"
        ],
    )

    numeric_cols = [
        "open", "high", "low", "close", "volume",
        "qav", "num_trades", "taker_base_vol", "taker_quote_vol"
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    return df

def fetch_funding_history(start_time, end_time):
    """
    Загружает исторический funding Binance.
    Funding обычно публикуется раз в 8 часов.
    """
    rows = []
    cursor = int(start_time)

    while cursor < end_time:
        params = {
            "symbol": SYMBOL,
            "startTime": cursor,
            "endTime": int(end_time),
            "limit": 1000,
        }

        r = requests.get(
            f"{BINANCE_URL}/fapi/v1/fundingRate",
            params=params,
            timeout=20,
        )
        r.raise_for_status()
        batch = r.json()

        if not batch:
            break

        rows.extend(batch)

        last_time = int(batch[-1]["fundingTime"])
        if last_time <= cursor:
            break

        cursor = last_time + 1
        print(f"Funding записей загружено: {len(rows)}")
        time.sleep(0.15)

        if len(batch) < 1000:
            break

    if not rows:
        return pd.DataFrame(columns=["fundingTime", "funding_rate"])

    df = pd.DataFrame(rows)
    df["fundingTime"] = pd.to_numeric(df["fundingTime"], errors="coerce")
    df["funding_rate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    df = df[["fundingTime", "funding_rate"]]
    df = df.drop_duplicates(subset=["fundingTime"]).sort_values("fundingTime")
    return df

def build_training_frame():
    candles = fetch_1h_history(total_candles=17520)

    funding = fetch_funding_history(
        start_time=int(candles["time"].iloc[0]),
        end_time=int(candles["time"].iloc[-1]),
    )

    # Подтягиваем последнее известное значение funding к каждой часовой свече
    df = pd.merge_asof(
        candles.sort_values("time"),
        funding.sort_values("fundingTime"),
        left_on="time",
        right_on="fundingTime",
        direction="backward",
    )

    df["funding_rate"] = df["funding_rate"].fillna(0.0)

    # Feature Engineering — строго по Gemini
    df["returns"] = df["close"].pct_change()
    df["volatility"] = df["returns"].rolling(12).std()
    df["volume_change"] = df["volume"].pct_change()

    # Target — цена через 12 часов выросла более чем на 1.5%
    future_return = (df["close"].shift(-12) - df["close"]) / df["close"]
    df["target"] = (future_return > 0.015).astype(int)

    df = df.dropna().reset_index(drop=True)
    return df

def train():
    df = build_training_frame()

    features = [
        "returns",
        "volatility",
        "volume_change",
        "funding_rate",
    ]

    X = df[features]
    y = df["target"]

    print(f"Строк для обучения: {len(df)}")
    print(f"Доля target=1: {y.mean():.4f}")

    model = CatBoostClassifier(
        iterations=500,
        depth=6,
        learning_rate=0.03,
        loss_function="Logloss",
        verbose=100,
    )

    model.fit(X, y)
    model.save_model("catboost_eth_model.cbm")

    # Также сохраняем обучающий датасет для контроля
    df.to_csv("crypto_historical_data.csv", index=False)

    print("ГОТОВО")
    print("Создан файл: catboost_eth_model.cbm")
    print("Создан файл: crypto_historical_data.csv")

if __name__ == "__main__":
    train()
