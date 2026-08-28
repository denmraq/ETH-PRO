import pandas as pd
from catboost import CatBoostClassifier

# 1. Загрузка исторических данных (свечи + фандинг + OI)
# В продакшене данные качаются из архива биржи или базы.
df = pd.read_csv("crypto_historical_data.csv")

# 2. Feature Engineering
df["returns"] = df["close"].pct_change()
df["volatility"] = df["returns"].rolling(12).std()
df["volume_change"] = df["volume"].pct_change()
df["funding_rate"] = df["funding_rate"]

# 3. Target: 1 — цена выросла через 12ч более чем на 1.5%, 0 — нет
future_return = (df["close"].shift(-12) - df["close"]) / df["close"]
df["target"] = (future_return > 0.015).astype(int)

df = df.dropna()

features = ["returns", "volatility", "volume_change", "funding_rate"]
X = df[features]
y = df["target"]

# 4. Обучение CatBoost
model = CatBoostClassifier(
    iterations=500,
    depth=6,
    learning_rate=0.03,
    loss_function="Logloss",
)
model.fit(X, y, verbose=False)

# 5. Сохранение обученной модели
model.save_model("catboost_eth_12h.cbm")
print("Модель успешно обучена и сохранена в catboost_eth_12h.cbm")
