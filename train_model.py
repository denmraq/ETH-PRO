import time
from pathlib import Path
import requests
import numpy as np
import pandas as pd
import joblib
from lightgbm import LGBMClassifier
from sklearn.metrics import accuracy_score, brier_score_loss
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit
from ml.features import build_features, binary_target, future_return, FEATURE_COLUMNS

OKX = "https://www.okx.com"
INST = "ETH-USDT-SWAP"
MODEL_PATH = "ml/eth_direction_lgbm.joblib"
REPORT_PATH = "ml/walk_forward_report.json"

def okx_get(path, params=None):
    r = requests.get(f"{OKX}{path}", params=params or {}, timeout=20,
                     headers={"User-Agent":"ETH-RADAR/2.0"})
    r.raise_for_status()
    p = r.json()
    if p.get("code") != "0":
        raise RuntimeError(p.get("msg") or str(p))
    return p.get("data", [])

def history_1h(target_bars=9000):
    rows, after = [], None
    while len(rows) < target_bars:
        params = {"instId": INST, "bar": "1H", "limit": "100"}
        if after:
            params["after"] = after
        chunk = okx_get("/api/v5/market/history-candles", params)
        if not chunk:
            break
        rows.extend(chunk)
        after = chunk[-1][0]
        time.sleep(0.08)
        if len(chunk) < 100:
            break
    cols = ["t","o","h","l","c","v","vol_ccy","qav","confirm"]
    df = pd.DataFrame(rows, columns=cols).drop_duplicates("t")
    for c in ["t","o","h","l","c","v","qav","confirm"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("t").reset_index(drop=True)
    return df[df["confirm"] == 1].copy()

def train_model():
    df = history_1h()
    X = build_features(df, funding_rate=0.0)
    y = binary_target(df, 12)
    ret = future_return(df, 12)

    data = X.copy()
    data["target"] = y
    data["future_return"] = ret
    data = data.dropna().reset_index(drop=True)

    X = data[FEATURE_COLUMNS]
    y = data["target"].astype(int)

    splitter = TimeSeriesSplit(n_splits=6, gap=12)
    fold_results = []
    oof_p, oof_y = [], []

    for fold, (tr, va) in enumerate(splitter.split(X), 1):
        model = LGBMClassifier(
            n_estimators=500,
            learning_rate=0.025,
            num_leaves=24,
            max_depth=-1,
            subsample=0.85,
            colsample_bytree=0.85,
            random_state=42,
        )
        model.fit(X.iloc[tr], y.iloc[tr])
        p = model.predict_proba(X.iloc[va])[:,1]
        pred = (p > 0.5).astype(int)
        fold_results.append({
            "fold": fold,
            "accuracy": float(accuracy_score(y.iloc[va], pred)),
            "brier": float(brier_score_loss(y.iloc[va], p)),
            "n": int(len(va)),
        })
        oof_p.extend(p.tolist())
        oof_y.extend(y.iloc[va].tolist())

    base_model = LGBMClassifier(
        n_estimators=650,
        learning_rate=0.02,
        num_leaves=24,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=42,
    )
    # Time-series split logic above is kept for validation.
    # Final probabilities are calibrated to make 60/70/80% more meaningful.
    cut = max(300, int(len(X) * 0.82))
    base_model.fit(X.iloc[:cut], y.iloc[:cut])
    calibrated = CalibratedClassifierCV(base_model, method="sigmoid", cv="prefit")
    calibrated.fit(X.iloc[cut:], y.iloc[cut:])
    final_model = calibrated
    Path("ml").mkdir(exist_ok=True)
    joblib.dump(final_model, MODEL_PATH)

    report = {
        "rows": int(len(X)),
        "folds": fold_results,
        "overall_accuracy": float(accuracy_score(oof_y, (np.array(oof_p)>0.5).astype(int))),
        "overall_brier": float(brier_score_loss(oof_y, oof_p)),
        "target": "Price(t+12h) > Price(t)",
    }
    import json
    Path(REPORT_PATH).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(report)
    return report

if __name__ == "__main__":
    train_model()
