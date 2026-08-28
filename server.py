import os, json, threading, xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import numpy as np
import pandas as pd
import joblib
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from deep_translator import GoogleTranslator

from ml.features import build_features, FEATURE_COLUMNS
from ml.risk import risk_plan

app = FastAPI(title="ETH Radar V2 - Calibrated Direction Engine")

OKX = "https://www.okx.com"
INST = "ETH-USDT-SWAP"
RSS = "https://app.chaingpt.org/rssfeeds-ethereum.xml"
MODEL_PATH = "ml/eth_direction_lgbm.joblib"
REPORT_PATH = "ml/walk_forward_report.json"

model = joblib.load(MODEL_PATH) if Path(MODEL_PATH).exists() else None
model_training = False
translation_cache = {}

def okx_get(path, params=None):
    r = requests.get(f"{OKX}{path}", params=params or {}, timeout=20,
                     headers={"User-Agent":"ETH-RADAR/2.0"})
    r.raise_for_status()
    p = r.json()
    if p.get("code") != "0":
        raise RuntimeError(p.get("msg") or str(p))
    return p.get("data", [])

def frame(rows):
    cols = ["t","o","h","l","c","v","vol_ccy","qav","confirm"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["t","o","h","l","c","v","qav","confirm"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("t").reset_index(drop=True)

def live_market():
    h1 = frame(okx_get("/api/v5/market/candles",
        {"instId":INST,"bar":"1H","limit":"120"}))
    m15 = frame(okx_get("/api/v5/market/candles",
        {"instId":INST,"bar":"15m","limit":"60"}))
    book = okx_get("/api/v5/market/books", {"instId":INST,"sz":"50"})[0]
    funding = okx_get("/api/v5/public/funding-rate", {"instId":INST})
    f = float(funding[0].get("fundingRate") or 0.0) if funding else 0.0

    bids = [(float(x[0]), float(x[1])) for x in book.get("bids", [])]
    asks = [(float(x[0]), float(x[1])) for x in book.get("asks", [])]
    bid_vol = sum(s for _,s in bids[:20])
    ask_vol = sum(s for _,s in asks[:20])
    denom = bid_vol + ask_vol
    ofi = (bid_vol - ask_vol) / denom if denom else 0.0

    if bids and asks and (bids[0][1] + asks[0][1]) > 0:
        micro = (asks[0][0]*bids[0][1] + bids[0][0]*asks[0][1]) / (bids[0][1]+asks[0][1])
        mid = (bids[0][0]+asks[0][0])/2
        micro_edge = (micro-mid)/mid if mid else 0.0
    else:
        micro_edge = 0.0

    return m15, h1, f, ofi, micro_edge

def ensure_model():
    global model_training, model
    if model is not None or model_training:
        return
    model_training = True
    def worker():
        global model_training, model
        try:
            from train_model import train_model
            train_model()
            model = joblib.load(MODEL_PATH)
        finally:
            model_training = False
    threading.Thread(target=worker, daemon=True).start()

@app.on_event("startup")
def startup():
    ensure_model()

def translate_ru(text):
    if not text:
        return ""
    if text in translation_cache:
        return translation_cache[text]
    try:
        ru = GoogleTranslator(source="auto", target="ru").translate(text)
    except Exception:
        ru = text
    translation_cache[text] = ru
    return ru

@app.get("/api/health")
def health():
    return {"status":"ok","model_loaded":model is not None,"model_training":model_training}

@app.get("/api/v2/predict")
def predict():
    m15, h1, funding, ofi, micro_edge = live_market()
    closed = h1[h1["confirm"] == 1].copy()
    if len(closed) < 80:
        raise RuntimeError("Недостаточно закрытых 1H свечей")

    price = float(closed["c"].iloc[-1])
    X = build_features(closed, funding_rate=funding, ofi=ofi, microprice_edge=micro_edge)
    row = X.iloc[[-1]]

    if model is None:
        p12 = 0.5
    else:
        p12 = float(model.predict_proba(row[FEATURE_COLUMNS])[0][1])

    # 1H microstructure model-like score from OFI + microprice + recent realized return
    last4 = m15[m15["confirm"] == 1].tail(4)
    ret1h = float(last4["c"].iloc[-1] / last4["c"].iloc[0] - 1.0) if len(last4)>=2 else 0.0
    raw1 = 0.5 + 0.20*np.tanh(ofi*2.5) + 0.20*np.tanh(micro_edge*2500) + 0.10*np.tanh(ret1h*40)
    p1 = float(np.clip(raw1, 0.01, 0.99))

    # 24H ensemble: 12H ML + structural trend + funding
    ema24 = closed["c"].ewm(span=24, adjust=False).mean().iloc[-1]
    ema72 = closed["c"].ewm(span=72, adjust=False).mean().iloc[-1]
    trend_bias = 0.08 if ema24 > ema72 else -0.08
    funding_bias = -0.06 if funding > 0.0003 else (0.06 if funding < -0.0001 else 0.0)
    p24 = float(np.clip(0.60*p12 + 0.40*(0.5+trend_bias+funding_bias), 0.01, 0.99))

    rv = float(np.log(closed["c"]).diff().rolling(24).std().iloc[-1] or 0.0)

    def pack(p, horizon):
        direction = "LONG" if p > 0.5 else "SHORT"
        chosen = p if direction=="LONG" else 1-p
        # approximate expected return using recent horizon-scaled volatility
        exp_ret = (chosen-0.5)*2.0*rv*np.sqrt(max(1,horizon))
        ci = 1.96*rv*np.sqrt(max(1,horizon))
        risk = risk_plan(price, p, rv, direction)
        return {
            "direction":direction,
            "probability":round(chosen*100,1),
            "note":"Модельная вероятность, не гарантированный win-rate",
            "raw_probability_up":round(p*100,1),
            "expected_return_pct":round(exp_ret*100,2),
            "confidence_interval_pct":[round((exp_ret-ci)*100,2), round((exp_ret+ci)*100,2)],
            "risk":risk,
        }

    return {
        "status":"ok","symbol":"ETH/USDT","price":round(price,2),
        "source":"OKX ETH-USDT-SWAP",
        "method":"Binary direction + calibrated-style walk-forward validation",
        "horizon_1h":pack(p1,1),
        "horizon_12h":pack(p12,12),
        "horizon_24h":pack(p24,24),
        "features":{
            "ofi":round(ofi,4),
            "microprice_edge":round(micro_edge,6),
            "funding_rate":funding,
            "realized_volatility":round(rv,6),
        }
    }

@app.get("/api/v2/validation")
def validation():
    if Path(REPORT_PATH).exists():
        return json.loads(Path(REPORT_PATH).read_text(encoding="utf-8"))
    return {"status":"not_ready"}

@app.get("/api/v2/news")
def news():
    r = requests.get(RSS, timeout=20, headers={"User-Agent":"ETH-RADAR/2.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.findall(".//item")[:6]:
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        text = (title+" "+desc).lower()
        pos = sum(k in text for k in ["approval","inflow","adoption","surge","rally","growth","institutional"])
        neg = sum(k in text for k in ["hack","exploit","outflow","lawsuit","ban","crash","liquidation"])
        sentiment = "BULLISH" if pos>neg else ("BEARISH" if neg>pos else "NEUTRAL")
        out.append({
            "title":translate_ru(title),
            "link":link,
            "published":pub,
            "sentiment":sentiment,
        })
    return {"status":"ok","source":"ChainGPT Ethereum RSS","articles":out}

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")
