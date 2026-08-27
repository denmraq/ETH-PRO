
from __future__ import annotations
import time
import threading
from state_store import save_flow_trades, flow_window as persistent_flow_window
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional
import requests
import pandas as pd
import numpy as np

BASE_URL = "https://api.bybit.com"
BINANCE_URL = "https://fapi.binance.com"
OKX_URL = "https://www.okx.com"
COINBASE_URL = "https://api.exchange.coinbase.com"
OKX_INST = "ETH-USDT-SWAP"
COINBASE_PRODUCT = "ETH-USD"
SYMBOL = "ETHUSDT"
CATEGORY = "linear"
TIMEOUT = 12
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ETH-Entry-Radar-PRO/1.0"})

_BYBIT_INTERVALS = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
}
_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}
_OI_INTERVALS = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

def _get(path: str, params=None):
    r = SESSION.get(BASE_URL + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}")
    return data

def _binance_get(path: str, params=None):
    r = SESSION.get(BINANCE_URL + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _okx_get(path: str, params=None):
    r = SESSION.get(OKX_URL + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and str(data.get("code", "0")) != "0":
        raise RuntimeError(f"OKX API error {data.get('code')}: {data.get('msg')}")
    return data

def _coinbase_get(path: str, params=None):
    r = SESSION.get(COINBASE_URL + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def _okx_bar(interval: str) -> str:
    return {"1m":"1m","5m":"5m","15m":"15m","1h":"1H","4h":"4H"}[interval]

def _coinbase_granularity(interval: str) -> int:
    return {"1m":60,"5m":300,"15m":900,"1h":3600,"4h":14400}[interval]

def klines(interval: str, limit: int = 300, closed_only: bool = True) -> pd.DataFrame:
    if interval not in _BYBIT_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    df=None
    errors=[]
    # 1) Bybit perpetual
    try:
        data = _get("/v5/market/kline", {"category":CATEGORY,"symbol":SYMBOL,"interval":_BYBIT_INTERVALS[interval],"limit":min(int(limit),1000)})
        raw=data.get("result",{}).get("list",[])
        df=pd.DataFrame(raw,columns=["open_time","open","high","low","close","volume","quote_volume"])
    except Exception as e:
        errors.append(f"Bybit:{type(e).__name__}")
    # 2) OKX ETH-USDT perpetual swap
    if df is None or not len(df):
        try:
            need=min(int(limit),300); rows=[]; after=None
            while len(rows)<need:
                params={"instId":OKX_INST,"bar":_okx_bar(interval),"limit":min(100,need-len(rows))}
                if after: params["after"]=after
                data=_okx_get("/api/v5/market/candles",params)
                batch=data.get("data",[])
                if not batch: break
                rows.extend(batch); after=batch[-1][0]
                if len(batch)<params["limit"]: break
            raw=[[r[0],r[1],r[2],r[3],r[4],r[5],r[7] if len(r)>7 else r[6]] for r in rows]
            df=pd.DataFrame(raw,columns=["open_time","open","high","low","close","volume","quote_volume"])
        except Exception as e:
            errors.append(f"OKX:{type(e).__name__}"); df=None
    # 3) Binance perpetual
    if df is None or not len(df):
        try:
            raw=_binance_get("/fapi/v1/klines",{"symbol":SYMBOL,"interval":interval,"limit":min(int(limit),1500)})
            df=pd.DataFrame([[r[0],r[1],r[2],r[3],r[4],r[5],r[7]] for r in raw],columns=["open_time","open","high","low","close","volume","quote_volume"])
        except Exception as e:
            errors.append(f"Binance:{type(e).__name__}"); df=None
    # 4) Coinbase spot. Keeps the engine alive if derivative venues block Render egress.
    if df is None or not len(df):
        try:
            raw=_coinbase_get(f"/products/{COINBASE_PRODUCT}/candles",{"granularity":_coinbase_granularity(interval)})
            # Coinbase: [time, low, high, open, close, volume], newest-first
            raw=raw[:min(int(limit),300)]
            df=pd.DataFrame([[int(r[0])*1000,r[3],r[2],r[1],r[4],r[5],float(r[4])*float(r[5])] for r in raw],columns=["open_time","open","high","low","close","volume","quote_volume"])
        except Exception as e:
            errors.append(f"Coinbase:{type(e).__name__}")
            raise RuntimeError("All market candle sources failed: "+", ".join(errors)) from e
    if df is None or not len(df):
        raise RuntimeError("No candle data returned")
    for c in ["open","high","low","close","volume","quote_volume"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")
    df["open_time"]=pd.to_datetime(pd.to_numeric(df["open_time"]),unit="ms",utc=True)
    df=df.dropna(subset=["open_time","open","high","low","close"]).sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    df["close_time"]=df["open_time"]+pd.to_timedelta(_INTERVAL_MS[interval],unit="ms")
    for c in ["trades","taker_buy_base","taker_buy_quote","ignore"]: df[c]=np.nan
    if closed_only:
        df=df[df["close_time"]<=pd.Timestamp.now(tz="UTC")].copy()
    return df.reset_index(drop=True)

def live_price() -> float:
    for fn in (
        lambda: float(_get("/v5/market/tickers",{"category":CATEGORY,"symbol":SYMBOL})["result"]["list"][0]["lastPrice"]),
        lambda: float(_okx_get("/api/v5/market/ticker",{"instId":OKX_INST})["data"][0]["last"]),
        lambda: float(_binance_get("/fapi/v1/ticker/price",{"symbol":SYMBOL})["price"]),
        lambda: float(_coinbase_get(f"/products/{COINBASE_PRODUCT}/ticker")["price"]),
    ):
        try: return fn()
        except Exception: pass
    raise RuntimeError("All live-price sources failed")

def indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
    delta = x["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    x["rsi14"] = 100 - 100/(1+rs)
    tr = pd.concat([
        x["high"]-x["low"],
        (x["high"]-x["close"].shift()).abs(),
        (x["low"]-x["close"].shift()).abs()
    ], axis=1).max(axis=1)
    x["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()
    x["atr_pct"] = x["atr14"] / x["close"] * 100
    x["prior20_high"] = x["high"].shift(1).rolling(20).max()
    x["prior20_low"] = x["low"].shift(1).rolling(20).min()
    x["ret1"] = x["close"].pct_change() * 100
    x["ret4"] = x["close"].pct_change(4) * 100
    return x

def open_interest_hist(period="5m", limit=30):
    interval_time=_OI_INTERVALS.get(period,"5min")
    try:
        data=_get("/v5/market/open-interest",{"category":CATEGORY,"symbol":SYMBOL,"intervalTime":interval_time,"limit":min(int(limit),200)})
        raw=data.get("result",{}).get("list",[]); df=pd.DataFrame(raw)
        if len(df):
            df["sumOpenInterest"]=pd.to_numeric(df["openInterest"],errors="coerce"); df["timestamp"]=pd.to_datetime(pd.to_numeric(df["timestamp"]),unit="ms",utc=True)
            return df.sort_values("timestamp").reset_index(drop=True)
    except Exception: pass
    # OKX public OI is a current snapshot, not a 5m history. We do not fake history.
    try:
        raw=_binance_get("/futures/data/openInterestHist",{"symbol":SYMBOL,"period":{"5m":"5m","15m":"15m","30m":"30m","1h":"1h","4h":"4h","1d":"1d"}.get(period,"5m"),"limit":min(int(limit),500)})
        df=pd.DataFrame(raw)
        if len(df):
            df["sumOpenInterest"]=pd.to_numeric(df["sumOpenInterest"],errors="coerce"); df["timestamp"]=pd.to_datetime(pd.to_numeric(df["timestamp"]),unit="ms",utc=True)
            return df.sort_values("timestamp").reset_index(drop=True)
    except Exception: pass
    return pd.DataFrame(columns=["sumOpenInterest","timestamp"])

def premium_index():
    try:
        rows=_get("/v5/market/funding/history",{"category":CATEGORY,"symbol":SYMBOL,"limit":1}).get("result",{}).get("list",[])
        if rows: return {"lastFundingRate":rows[0].get("fundingRate","0"),"source":"Bybit"}
    except Exception: pass
    try:
        rows=_okx_get("/api/v5/public/funding-rate",{"instId":OKX_INST}).get("data",[])
        if rows: return {"lastFundingRate":rows[0].get("fundingRate","0"),"source":"OKX"}
    except Exception: pass
    try:
        data=_binance_get("/fapi/v1/premiumIndex",{"symbol":SYMBOL}); return {"lastFundingRate":data.get("lastFundingRate","0"),"source":"Binance"}
    except Exception: return {"lastFundingRate":"0","source":"Unavailable"}

def agg_trades(limit=1000):
    try:
        raw=_get("/v5/market/recent-trade",{"category":CATEGORY,"symbol":SYMBOL,"limit":min(int(limit),1000)}).get("result",{}).get("list",[])
        df=pd.DataFrame(raw)
        if len(df):
            df["p"]=pd.to_numeric(df["price"],errors="coerce"); df["q"]=pd.to_numeric(df["size"],errors="coerce"); df["T"]=pd.to_datetime(pd.to_numeric(df["time"]),unit="ms",utc=True)
            df["signed_quote"]=np.where(df["side"].astype(str).str.lower().eq("buy"),df["p"]*df["q"],-df["p"]*df["q"]); return df.sort_values("T").reset_index(drop=True)
    except Exception: pass
    try:
        rows=_okx_get("/api/v5/market/trades",{"instId":OKX_INST,"limit":min(int(limit),500)}).get("data",[])
        df=pd.DataFrame(rows)
        if len(df):
            df["p"]=pd.to_numeric(df["px"],errors="coerce"); df["q"]=pd.to_numeric(df["sz"],errors="coerce"); df["T"]=pd.to_datetime(pd.to_numeric(df["ts"]),unit="ms",utc=True)
            df["signed_quote"]=np.where(df["side"].astype(str).str.lower().eq("buy"),df["p"]*df["q"],-df["p"]*df["q"]); df["execId"]=df.get("tradeId",pd.Series(range(len(df)))).astype(str)
            return df.sort_values("T").reset_index(drop=True)
    except Exception: pass
    try:
        raw=_binance_get("/fapi/v1/aggTrades",{"symbol":SYMBOL,"limit":min(int(limit),1000)}); df=pd.DataFrame(raw)
        if len(df):
            df["p"]=pd.to_numeric(df["p"],errors="coerce"); df["q"]=pd.to_numeric(df["q"],errors="coerce"); df["T"]=pd.to_datetime(pd.to_numeric(df["T"]),unit="ms",utc=True)
            df["side"]=np.where(df["m"].astype(bool),"Sell","Buy"); df["signed_quote"]=np.where(df["m"].astype(bool),-(df["p"]*df["q"]),(df["p"]*df["q"])); df["execId"]=df.get("a",pd.Series(range(len(df)))).astype(str)
            return df.sort_values("T").reset_index(drop=True)
    except Exception: pass
    # Coinbase spot trades are the final flow fallback. 'side' is maker side, so aggressor is opposite.
    try:
        raw=_coinbase_get(f"/products/{COINBASE_PRODUCT}/trades",{"limit":min(int(limit),1000)}); df=pd.DataFrame(raw)
        if len(df):
            df["p"]=pd.to_numeric(df["price"],errors="coerce"); df["q"]=pd.to_numeric(df["size"],errors="coerce"); df["T"]=pd.to_datetime(df["time"],utc=True,errors="coerce")
            maker=df["side"].astype(str).str.lower(); df["side"]=np.where(maker.eq("sell"),"Buy","Sell"); df["signed_quote"]=np.where(maker.eq("sell"),df["p"]*df["q"],-(df["p"]*df["q"])); df["execId"]=df.get("trade_id",pd.Series(range(len(df)))).astype(str)
            return df.sort_values("T").reset_index(drop=True)
    except Exception: pass
    return pd.DataFrame(columns=["p","q","T","side","signed_quote","execId"])

# Persistent multi-window Flow. The background monitor keeps this populated even
# while the iPhone is locked; a persistent hosting disk preserves the window across restarts.
def _update_flow_buffer(trades: pd.DataFrame):
    if trades is None or not len(trades): return
    rows=[]
    for _,row in trades.iterrows():
        try:
            ts=row["T"].timestamp(); eid=str(row.get("execId") or f"{ts}-{row['p']}-{row['q']}-{row.get('side','')}")
            quote=abs(float(row["p"])*float(row["q"])); signed=float(row["signed_quote"])
            rows.append((eid,ts,signed,quote))
        except Exception: continue
    if rows: save_flow_trades(rows)

def flow_window(minutes:int):
    return persistent_flow_window(minutes)

def pct(a,b):
    if b == 0 or pd.isna(a) or pd.isna(b): return 0.0
    return (a/b - 1)*100

def clamp_score(x): return max(0, min(100, int(round(x))))

def classify_direction(long_score: int, short_score: int, tie_long: bool = True):
    """Forced directional output: LONG or SHORT only.

    V0.3.5 intentionally never returns WAIT/WATCH/NEUTRAL. The higher score wins.
    Exact score ties are broken by the latest closed 15m EMA20 side supplied by
    the caller, so the result is deterministic and still market-anchored.
    """
    gap = long_score - short_score
    if gap > 0:
        return "LONG"
    if gap < 0:
        return "SHORT"
    return "LONG" if tie_long else "SHORT"


def entry_zone(direction: str, price: float, ema20_15m: float, atr15: float):
    """Return an actionable entry band anchored to the last closed 15m EMA20.

    The band is a price range, not a timing instruction. If price is stretched,
    the zone stays near EMA20 rather than chasing the live price.
    """
    atr = max(float(atr15), 1e-9)
    anchor = float(ema20_15m)
    if direction == "LONG":
        low = anchor - 0.20 * atr
        high = anchor + 0.30 * atr
    elif direction == "SHORT":
        low = anchor - 0.30 * atr
        high = anchor + 0.20 * atr
    else:
        return None, None
    return min(low, high), max(low, high)

def adaptive_forward_plan(direction: str, price: float, entry_low: float, entry_high: float, stop: float,
                          base_targets: list[float], d1m: pd.DataFrame, d5: pd.DataFrame, d15: pd.DataFrame, d1: pd.DataFrame):
    """Roll stale targets forward and classify the current market stage.

    The original setup remains the reference, but once price has consumed one or
    more targets the active plan is rebuilt from the fast 1m/5m structure. This
    keeps every displayed TP ahead of the live price instead of showing already
    completed targets as if they were forecasts.
    """
    atr1=max(float(d1m.iloc[-1]["atr14"]),1e-9)
    atr5=max(float(d5.iloc[-1]["atr14"]),1e-9)
    atr15=max(float(d15.iloc[-1]["atr14"]),1e-9)
    atr1h=max(float(d1.iloc[-1]["atr14"]),1e-9)
    eps=max(0.08*atr5, 0.02*atr15)
    t=[float(x) for x in base_targets if x is not None]
    if direction=="LONG":
        hit=sum(price >= x-eps for x in t)
    else:
        hit=sum(price <= x+eps for x in t)

    # Recent fast range is used to distinguish a clean breakout from consolidation.
    recent=d5.tail(6)
    rhi=float(recent["high"].max()); rlo=float(recent["low"].min())
    range_atr=(rhi-rlo)/atr5
    fast_close=float(d5.iloc[-1]["close"]); fast_ema=float(d5.iloc[-1]["ema20"])

    stage="SETUP"
    if hit==1: stage="TP1_HIT"
    elif hit==2: stage="TP2_HIT"
    elif hit>=3: stage="TARGET_BREAKOUT"
    if hit>=3 and range_atr <= 1.35:
        if direction=="LONG" and rlo > t[-1]-0.35*atr5: stage="CONSOLIDATION_ABOVE"
        if direction=="SHORT" and rhi < t[-1]+0.35*atr5: stage="CONSOLIDATION_BELOW"

    # Before targets are consumed, preserve the original setup.
    if hit==0:
        return {"stage":stage,"targets_hit":0,"entry_low":entry_low,"entry_high":entry_high,"stop":stop,
                "targets":t[:3],"rollover":False}

    # Re-anchor the continuation setup to the fast EMA / local structure.
    anchor=float(d5.iloc[-1]["ema20"])

    # V1.6 forward horizon floor.  The old implementation could choose three nearby
    # historical swing levels only a few dollars beyond the live price.  Those are
    # useful micro-resistances, but not useful as the *next forecast ladder*.  Every
    # rolled target must now clear a minimum live-price distance derived from 15m/1H
    # volatility and a small percentage floor.
    horizon_abs=[
        max(0.75*atr15, 0.25*atr1h, abs(price)*0.0015),
        max(1.50*atr15, 0.50*atr1h, abs(price)*0.0030),
        max(2.40*atr15, 0.80*atr1h, abs(price)*0.0050),
    ]
    min_spacing=max(0.35*atr15, 0.10*atr1h, abs(price)*0.0006)

    def _pick_forward(candidates, sign):
        ordered=sorted({float(x) for x in candidates}, reverse=(sign<0))
        out=[]
        for i,dist in enumerate(horizon_abs):
            threshold=price + sign*dist
            if sign>0:
                eligible=[x for x in ordered if x >= threshold and (not out or x >= out[-1]+min_spacing)]
                chosen=eligible[0] if eligible else threshold
                if out and chosen < out[-1]+min_spacing: chosen=out[-1]+min_spacing
            else:
                eligible=[x for x in ordered if x <= threshold and (not out or x <= out[-1]-min_spacing)]
                chosen=eligible[0] if eligible else threshold
                if out and chosen > out[-1]-min_spacing: chosen=out[-1]-min_spacing
            out.append(float(chosen))
        return out

    if direction=="LONG":
        new_low=anchor-0.15*atr5; new_high=anchor+0.25*atr5
        local_stop=rlo-0.18*atr5
        new_stop=min(local_stop, (new_low+new_high)/2-0.75*atr5)
        cands=[x for x in t if x > price+eps]
        cands += [float(x) for x in d1["high"].tail(120).values if float(x) > price+eps]
        base=max(price, rhi, fast_close)
        cands += [base+0.8*atr15, base+1.6*atr15, base+2.5*atr15, base+3.4*atr15]
        future=_pick_forward(cands, +1)
    else:
        new_low=anchor-0.25*atr5; new_high=anchor+0.15*atr5
        local_stop=rhi+0.18*atr5
        new_stop=max(local_stop, (new_low+new_high)/2+0.75*atr5)
        cands=[x for x in t if x < price-eps]
        cands += [float(x) for x in d1["low"].tail(120).values if float(x) < price-eps]
        base=min(price, rlo, fast_close)
        cands += [base-0.8*atr15, base-1.6*atr15, base-2.5*atr15, base-3.4*atr15]
        future=_pick_forward(cands, -1)
    return {"stage":stage,"targets_hit":hit,"entry_low":min(new_low,new_high),"entry_high":max(new_low,new_high),
            "stop":new_stop,"targets":future[:3],"rollover":True}

def time_to_event_analogs(hist15: pd.DataFrame, direction: str, stop_atr: float, target_atr: float,
                          horizon_bars: int = 16, min_analogs: int = 30, max_analogs: int = 60):
    """
    V0.3.7 historical analogue estimator.

    Uses nearest historical 15m states rather than brittle hard filters. It only
    compares already-closed historical bars that have a complete forward horizon.
    The selected sample is therefore non-empty whenever sufficient history exists.

    This remains descriptive historical statistics, not a guaranteed forecast.
    """
    x = indicators(hist15)
    required = ["open","high","low","close","ema20","ema50","rsi14","atr14","atr_pct","ret4"]
    x = x.dropna(subset=required).reset_index(drop=True)

    # Need enough warm-up plus forward bars for honest historical outcomes.
    max_i = len(x) - horizon_bars - 2
    if max_i <= 80:
        return {"analog_count":0,"p_tp_first":None,"p_sl_first":None,"p_neither":None,
                "tp_time_p25_min":None,"tp_time_median_min":None,"tp_time_p75_min":None,
                "first_event_median_min":None}

    cur = x.iloc[-1]
    cur_side = 1 if cur["close"] >= cur["ema20"] else -1
    cur_slope = 1 if cur["ema20"] >= x.iloc[-4]["ema20"] else -1

    # Current feature vector. Distances are scaled so no single feature dominates.
    cur_rsi = float(cur["rsi14"])
    cur_atrp = max(float(cur["atr_pct"]), 1e-6)
    cur_ret4 = float(cur["ret4"])
    cur_ema_gap_atr = float((cur["close"] - cur["ema20"]) / max(cur["atr14"], 1e-9))
    cur_trend_gap_atr = float((cur["ema20"] - cur["ema50"]) / max(cur["atr14"], 1e-9))

    ranked = []
    for i in range(60, max_i + 1):
        row = x.iloc[i]
        atr = max(float(row["atr14"]), 1e-9)
        side = 1 if row["close"] >= row["ema20"] else -1
        slope = 1 if row["ema20"] >= x.iloc[i-3]["ema20"] else -1

        # Prefer the chosen trade direction and same EMA context, but do not make
        # those conditions capable of collapsing the sample to zero.
        desired_side = 1 if direction == "LONG" else -1
        direction_penalty = 2.5 if side != desired_side else 0.0
        context_penalty = 0.75 if side != cur_side else 0.0
        slope_penalty = 0.50 if slope != cur_slope else 0.0

        ema_gap_atr = float((row["close"] - row["ema20"]) / atr)
        trend_gap_atr = float((row["ema20"] - row["ema50"]) / atr)

        dist = (
            abs(float(row["rsi14"]) - cur_rsi) / 12.0
            + abs(float(row["atr_pct"]) - cur_atrp) / max(cur_atrp, 0.15)
            + abs(float(row["ret4"]) - cur_ret4) / 0.8
            + abs(ema_gap_atr - cur_ema_gap_atr) / 1.2
            + abs(trend_gap_atr - cur_trend_gap_atr) / 1.5
            + direction_penalty + context_penalty + slope_penalty
        )
        ranked.append((dist, i))

    ranked.sort(key=lambda z: z[0])

    # First take same-direction candidates, then fill with closest states if needed.
    desired_side = 1 if direction == "LONG" else -1
    selected = []
    for dist, i in ranked:
        row = x.iloc[i]
        side = 1 if row["close"] >= row["ema20"] else -1
        if side == desired_side:
            selected.append(i)
        if len(selected) >= max_analogs:
            break

    if len(selected) < min_analogs:
        used = set(selected)
        for dist, i in ranked:
            if i not in used:
                selected.append(i)
                used.add(i)
            if len(selected) >= min(min_analogs, len(ranked)):
                break

    candidates = selected[:max_analogs]

    tp_first = sl_first = neither = 0
    tp_times = []
    first_times = []

    # Keep risk/target geometry sane even if current structural stop is unusually wide.
    stop_atr = float(np.clip(stop_atr, 0.45, 3.0))
    target_atr = float(np.clip(target_atr, 0.60, 5.0))

    for i in candidates:
        row = x.iloc[i]
        entry = float(row["close"])
        atr = max(float(row["atr14"]), 1e-9)

        if direction == "LONG":
            tp = entry + target_atr * atr
            sl = entry - stop_atr * atr
        else:
            tp = entry - target_atr * atr
            sl = entry + stop_atr * atr

        outcome = None
        event_bar = None
        for j in range(1, horizon_bars + 1):
            bar = x.iloc[i + j]
            hit_tp = (bar["high"] >= tp) if direction == "LONG" else (bar["low"] <= tp)
            hit_sl = (bar["low"] <= sl) if direction == "LONG" else (bar["high"] >= sl)

            # Conservative when both are touched inside one OHLC bar.
            if hit_tp and hit_sl:
                outcome, event_bar = "SL", j
                break
            if hit_sl:
                outcome, event_bar = "SL", j
                break
            if hit_tp:
                outcome, event_bar = "TP", j
                break

        if outcome == "TP":
            tp_first += 1
            tp_times.append(event_bar * 15)
            first_times.append(event_bar * 15)
        elif outcome == "SL":
            sl_first += 1
            first_times.append(event_bar * 15)
        else:
            neither += 1

    n = len(candidates)

    def q(vals, p):
        return int(round(float(np.quantile(vals, p)) / 15) * 15) if vals else None

    return {
        "analog_count": n,
        "p_tp_first": round(tp_first / n * 100, 1) if n else None,
        "p_sl_first": round(sl_first / n * 100, 1) if n else None,
        "p_neither": round(neither / n * 100, 1) if n else None,
        "tp_time_p25_min": q(tp_times, .25),
        "tp_time_median_min": q(tp_times, .50),
        "tp_time_p75_min": q(tp_times, .75),
        "first_event_median_min": q(first_times, .50),
    }

@dataclass
class Result:
    timestamp_utc: str
    symbol: str
    price: float
    last_closed_15m_utc: str
    regime: str
    bias: str
    entry_status: str
    stage: str
    market_stage: str
    targets_hit: int
    target_rollover: bool
    fast_bias: str
    data_confidence: int
    data_quality: list[str]
    long_score: int
    short_score: int
    signal: str
    entry_low: Optional[float]
    entry_high: Optional[float]
    stop: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    tp3: Optional[float]
    rr_tp2: Optional[float]
    funding_rate_pct: float
    oi_change_pct: float
    cvd_quote: float
    flow_5m_pct: Optional[float]
    flow_15m_pct: Optional[float]
    flow_5m_coverage_min: float
    flow_15m_coverage_min: float
    expiry_minutes: int
    entry_window: Optional[str]
    analog_count: int
    p_tp_first: Optional[float]
    p_sl_first: Optional[float]
    p_neither_4h: Optional[float]
    tp_time_p25_min: Optional[int]
    tp_time_median_min: Optional[int]
    tp_time_p75_min: Optional[int]
    reasons_long: list[str]
    reasons_short: list[str]
    vetoes: list[str]
    warnings: list[str]

    def to_dict(self): return asdict(self)

def analyze():
    # Closed candles for decisions = no repaint from the currently forming candle.
    d4 = indicators(klines("4h", 260, True))
    d1 = indicators(klines("1h", 300, True))
    d1m = indicators(klines("1m", 300, True))
    d15_raw = klines("15m", 1000, True)
    d15 = indicators(d15_raw)
    d5 = indicators(klines("5m", 400, True))
    price = live_price()

    ls=ss=0
    rl=[]; rs=[]; veto=[]; warnings=[]

    # 1) Higher-TF context, 20 points.
    for df,label,pts in [(d4,"4H",10),(d1,"1H",10)]:
        z=df.iloc[-1]
        if z["close"] > z["ema20"] > z["ema50"] and z["ema20"] > df.iloc[-4]["ema20"]:
            ls += pts; rl.append(f"{label}: bullish EMA structure (+{pts})")
        elif z["close"] < z["ema20"] < z["ema50"] and z["ema20"] < df.iloc[-4]["ema20"]:
            ss += pts; rs.append(f"{label}: bearish EMA structure (+{pts})")

    # 2) Closed 15m breakout only. Avoid current unfinished candle.
    z15=d15.iloc[-1]
    breakout_depth = 0.15 * float(z15["atr14"])
    if z15["close"] > z15["prior20_high"] + breakout_depth:
        ls += 5; rl.append("15m: confirmed close above prior range (+5)")
    elif z15["close"] < z15["prior20_low"] - breakout_depth:
        ss += 5; rs.append("15m: confirmed close below prior range (+5)")

    # 3) Multi-window Flow. Never pretend the latest 1000 executions are a 5m/15m window.
    trades=agg_trades(1000)
    _update_flow_buffer(trades)
    f5=flow_window(5); f15=flow_window(15)
    d5flow=f5["delta_pct"]; d15flow=f15["delta_pct"]
    # For backward compatibility with the current iPhone UI, cvd_quote is the
    # longest mature CVD window available (15m preferred, then 5m).
    cvd=float(f15["cvd_quote"] if f15["coverage_min"] >= 12.0 else f5["cvd_quote"])

    if d5flow is not None and f5["coverage_min"] >= 4.0:
        if d5flow > 6:
            ls += 8; rl.append(f"Flow 5m: aggressive BUY delta +{d5flow:.1f}% (+8)")
        elif d5flow < -6:
            ss += 8; rs.append(f"Flow 5m: aggressive SELL delta {d5flow:.1f}% (+8)")
    else:
        warnings.append(f"Flow 5m is warming up ({f5['coverage_min']:.1f}/4.0 min required for scoring).")

    if d15flow is not None and f15["coverage_min"] >= 12.0:
        if d15flow > 4:
            ls += 7; rl.append(f"Flow 15m confirms BUY +{d15flow:.1f}% (+7)")
        elif d15flow < -4:
            ss += 7; rs.append(f"Flow 15m confirms SELL {d15flow:.1f}% (+7)")
    else:
        warnings.append(f"Flow 15m is warming up ({f15['coverage_min']:.1f}/12.0 min required for scoring).")

    # 4) OI
    oi_ch=0.0
    oih=open_interest_hist("5m",30)
    if len(oih)>=7:
        oi_ch=pct(float(oih.iloc[-1]["sumOpenInterest"]),float(oih.iloc[-7]["sumOpenInterest"]))
        p_ch=pct(float(d5.iloc[-1]["close"]),float(d5.iloc[-7]["close"]))
        if oi_ch>.20 and p_ch>.10:
            pts=min(15,8+int(min(7,oi_ch*3))); ls+=pts
            rl.append(f"OI +{oi_ch:.2f}% with rising price (+{pts})")
        elif oi_ch>.20 and p_ch<-.10:
            pts=min(15,8+int(min(7,oi_ch*3))); ss+=pts
            rs.append(f"OI +{oi_ch:.2f}% with falling price (+{pts})")
        elif oi_ch<-.25:
            warnings.append("OI is falling: move may be driven by position closing rather than fresh positioning.")

    # 5) Nearby swing is context only, lower weight than old V0.2.
    swing_hi=float(d1["high"].iloc[-25:-1].max())
    swing_lo=float(d1["low"].iloc[-25:-1].min())
    up=(swing_hi/price-1)*100
    dn=(1-swing_lo/price)*100
    if 0<up<dn and up<1.0 and ls>=ss:
        ls+=5; rl.append(f"Nearby 1H upper swing {swing_hi:.2f} (+5 context)")
    elif 0<dn<up and dn<1.0 and ss>=ls:
        ss+=5; rs.append(f"Nearby 1H lower swing {swing_lo:.2f} (+5 context)")
    warnings.append("Liquidity is still a swing-level proxy; it is NOT a liquidation heatmap.")

    # 6) Momentum
    for df,label,pts in [(d15,"15m",6),(d5,"5m",4)]:
        z=df.iloc[-1]
        if z["close"]>z["ema20"] and z["ema20"]>df.iloc[-4]["ema20"]:
            ls+=pts; rl.append(f"{label}: momentum above rising EMA20 (+{pts})")
        elif z["close"]<z["ema20"] and z["ema20"]<df.iloc[-4]["ema20"]:
            ss+=pts; rs.append(f"{label}: momentum below falling EMA20 (+{pts})")

    # 7) Funding
    funding_info=premium_index()
    funding=float(funding_info.get("lastFundingRate",0))*100
    if funding>.03 and ss>=ls:
        ss+=5; rs.append(f"High positive funding {funding:.4f}% (+5 SHORT)")
    elif funding<-.03 and ls>=ss:
        ls+=5; rl.append(f"Negative funding {funding:.4f}% (+5 LONG)")

    # 8) RSI contextual
    r1=float(d1.iloc[-1]["rsi14"]); r15=float(d15.iloc[-1]["rsi14"])
    if 45<=r1<=68 and r15>52 and ls>ss:
        ls+=5; rl.append(f"RSI confirms LONG: 1H {r1:.1f}, 15m {r15:.1f} (+5)")
    elif 32<=r1<=55 and r15<48 and ss>ls:
        ss+=5; rs.append(f"RSI confirms SHORT: 1H {r1:.1f}, 15m {r15:.1f} (+5)")

    # 9) Closed-bar retest proxy
    atr15=float(d15.iloc[-1]["atr14"])
    prev=d15.iloc[-2]; cur=d15.iloc[-1]
    if abs(prev["low"]-prev["ema20"])<=.35*atr15 and cur["close"]>cur["ema20"] and cur["close"]>prev["close"]:
        ls+=10; rl.append("15m: EMA20 retest held (+10)")
    elif abs(prev["high"]-prev["ema20"])<=.35*atr15 and cur["close"]<cur["ema20"] and cur["close"]<prev["close"]:
        ss+=10; rs.append("15m: EMA20 rejection / retest down (+10)")

    # 10) Fast 1m sensitivity layer. Small weight, fast reaction; higher TFs still dominate.
    z1m=d1m.iloc[-1]
    fast_bias="NEUTRAL"
    one_min_gap=(float(z1m["close"])-float(z1m["ema20"]))/max(float(z1m["atr14"]),1e-9)
    if z1m["close"]>z1m["ema20"] and z1m["ema20"]>d1m.iloc[-4]["ema20"]:
        ls+=4; fast_bias="LONG"; rl.append("1m: fast momentum above rising EMA20 (+4)")
    elif z1m["close"]<z1m["ema20"] and z1m["ema20"]<d1m.iloc[-4]["ema20"]:
        ss+=4; fast_bias="SHORT"; rs.append("1m: fast momentum below falling EMA20 (+4)")
    # Extra 2 points only for an actual closed 1m range break, limiting noise.
    if z1m["close"] > z1m["prior20_high"] + 0.08*float(z1m["atr14"]):
        ls+=2; rl.append("1m: closed micro-breakout (+2)")
    elif z1m["close"] < z1m["prior20_low"] - 0.08*float(z1m["atr14"]):
        ss+=2; rs.append("1m: closed micro-breakdown (+2)")

    ls=clamp_score(ls); ss=clamp_score(ss)

    # Regime
    ema_gap=abs(float(d1.iloc[-1]["ema20"]/d1.iloc[-1]["ema50"]-1))*100
    atr_pct=float(d1.iloc[-1]["atr_pct"])
    if ema_gap>.8: regime="TREND"
    elif atr_pct<.45: regime="RANGE"
    else: regime="TRANSITION"

    # Direction-specific chaser vetoes. Fixed bug from V0.2.
    live_dist_atr=abs(price-float(cur["ema20"]))/(atr15 or 1)
    block_long = r15>76 or (price>cur["ema20"] and live_dist_atr>1.6)
    block_short = r15<24 or (price<cur["ema20"] and live_dist_atr>1.6)
    if r15>76: veto.append("BLOCK_LONG: 15m RSI overbought.")
    if r15<24: veto.append("BLOCK_SHORT: 15m RSI oversold.")
    if price>cur["ema20"] and live_dist_atr>1.6:
        veto.append(f"BLOCK_LONG: price is {live_dist_atr:.1f} ATR above 15m EMA20; do not chase.")
    if price<cur["ema20"] and live_dist_atr>1.6:
        veto.append(f"BLOCK_SHORT: price is {live_dist_atr:.1f} ATR below 15m EMA20; do not chase.")

    # V0.3.5: forced binary directional call — LONG or SHORT, never neutral/wait.
    tie_long = bool(float(cur["close"]) >= float(cur["ema20"]))
    signal = classify_direction(ls, ss, tie_long=tie_long)
    bias = signal
    stage = signal
    entry_status = signal
    direction = signal
    entry_window = None

    entry_low=entry_high=stop=tp1=tp2=tp3=rr=None
    stop_atr=0.9; target_atr=1.5
    if direction:
        rh=float(d15["high"].iloc[-12:-1].max())
        rlw=float(d15["low"].iloc[-12:-1].min())
        entry_low, entry_high = entry_zone(direction, price, float(cur["ema20"]), atr15)
        entry_mid=(entry_low+entry_high)/2
        if direction=="LONG":
            structural_stop = rlw - .15 * atr15
            atr_stop = entry_mid - .90 * atr15
            # Never leave STOP empty; use the safer of structure/ATR but cap extreme distance.
            stop = min(structural_stop, atr_stop)
            stop = max(stop, entry_mid - 2.50 * atr15)
            risk=entry_mid-stop
            tp1=entry_mid+risk; tp2=entry_mid+1.7*risk; tp3=entry_mid+2.5*risk
            if block_long:
                veto.append("LONG is stretched: use the stated entry band; do not chase above it.")
        else:
            structural_stop = rh + .15 * atr15
            atr_stop = entry_mid + .90 * atr15
            stop = max(structural_stop, atr_stop)
            stop = min(stop, entry_mid + 2.50 * atr15)
            risk=stop-entry_mid
            tp1=entry_mid-risk; tp2=entry_mid-1.7*risk; tp3=entry_mid-2.5*risk
            if block_short:
                veto.append("SHORT is stretched: use the stated entry band; do not chase below it.")
        if risk<=0:
            veto.append("Risk geometry is invalid; treat the directional call as context only.")
        else:
            rr=abs(tp2-entry_mid)/risk
            stop_atr=risk/atr15
            target_atr=abs(tp2-entry_mid)/atr15
            if risk/entry_mid>.025:
                veto.append("Structural stop exceeds 2.5% of entry midpoint.")
            if rr<1.5:
                veto.append(f"R:R {rr:.2f} is below 1.5.")

    market_stage="SETUP"; targets_hit=0; target_rollover=False
    if direction and all(x is not None for x in (entry_low,entry_high,stop,tp1,tp2,tp3)):
        plan=adaptive_forward_plan(direction,price,entry_low,entry_high,stop,[tp1,tp2,tp3],d1m,d5,d15,d1)
        market_stage=plan["stage"]; targets_hit=int(plan["targets_hit"]); target_rollover=bool(plan["rollover"])
        if target_rollover:
            entry_low=float(plan["entry_low"]); entry_high=float(plan["entry_high"]); stop=float(plan["stop"])
            tp1,tp2,tp3=[float(x) for x in plan["targets"]]
            entry_mid=(entry_low+entry_high)/2
            risk=abs(entry_mid-stop)
            rr=abs(tp2-entry_mid)/risk if risk>0 else None
            stop_atr=risk/atr15 if risk>0 else stop_atr
            target_atr=abs(tp2-entry_mid)/atr15 if risk>0 else target_atr
            warnings.append(f"Dynamic target rollover active: {targets_hit} prior target(s) already consumed; forward targets rebuilt with 15m/1H horizon floors.")

    analog = {"analog_count":0,"p_tp_first":None,"p_sl_first":None,"p_neither":None,
              "tp_time_p25_min":None,"tp_time_median_min":None,"tp_time_p75_min":None,"first_event_median_min":None}
    if direction:
        analog=time_to_event_analogs(d15_raw,direction,stop_atr,target_atr,16)

    # Dynamic expiry: minimum 45m, informed by historical first-event timing when available.
    first_med=analog.get("first_event_median_min")
    expiry=45 if first_med is None else int(max(45,min(180,round((first_med*.75)/15)*15)))

    # Data confidence is coverage/quality, not trade probability.
    quality=[]; confidence=0
    if all(len(df) >= 100 for df in (d4,d1,d15,d5,d1m)):
        confidence += 35; quality.append("Closed candles 4H/1H/15m/5m/1m: OK (+35)")
    if len(oih) >= 7:
        confidence += 15; quality.append("Open interest history: OK (+15)")
    else: quality.append("Open interest history: limited")
    
    if funding_info.get("source") != "Unavailable":
        confidence += 10; quality.append(f"Funding: OK via {funding_info.get('source')} (+10)")
    else:
        quality.append("Funding: unavailable (0 used only as neutral placeholder)")
    if f5["coverage_min"] >= 4.0 and f15["coverage_min"] >= 12.0:
        confidence += 15; quality.append("Multi-window Flow 5m/15m: mature (+15)")
    elif f5["coverage_min"] >= 4.0:
        confidence += 9; quality.append(f"Flow 5m ready; 15m warming {f15['coverage_min']:.1f}m (+9/15)")
    elif f5["coverage_min"] > 0:
        tf_pts=max(3,int(round(9*f5["coverage_min"]/4)))
        confidence += tf_pts; quality.append(f"Flow warming {f5['coverage_min']:.1f}m (+{tf_pts}/15)")
    else: quality.append("Flow: unavailable")
    ac=int(analog.get("analog_count") or 0)
    an_pts = 15 if ac >= 25 else int(round(15*min(ac,25)/25))
    confidence += an_pts
    quality.append(f"Historical analogs: {ac} (+{an_pts}/15)")
    if len(d15_raw) and pd.notna(d15_raw.iloc[-1]["close_time"]):
        confidence += 10; quality.append("Last closed 15m timestamp: OK (+10)")
    confidence=max(0,min(100,int(confidence)))

    rd=lambda x: round(float(x),2) if x is not None else None
    return Result(
        timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        symbol=SYMBOL, price=rd(price),
        last_closed_15m_utc=d15_raw.iloc[-1]["close_time"].isoformat(),
        regime=regime, bias=bias, entry_status=entry_status, stage=stage,
        market_stage=market_stage, targets_hit=targets_hit, target_rollover=target_rollover, fast_bias=fast_bias,
        data_confidence=confidence, data_quality=quality,
        long_score=ls, short_score=ss, signal=signal,
        entry_low=rd(entry_low), entry_high=rd(entry_high), stop=rd(stop),
        tp1=rd(tp1), tp2=rd(tp2), tp3=rd(tp3), rr_tp2=round(rr,2) if rr else None,
        funding_rate_pct=round(funding,5), oi_change_pct=round(oi_ch,3),
        cvd_quote=round(cvd,2),
        flow_5m_pct=f5.get("delta_pct"), flow_15m_pct=f15.get("delta_pct"),
        flow_5m_coverage_min=float(f5.get("coverage_min",0.0)),
        flow_15m_coverage_min=float(f15.get("coverage_min",0.0)),
        expiry_minutes=expiry, entry_window=entry_window,
        analog_count=int(analog["analog_count"]),
        p_tp_first=analog["p_tp_first"], p_sl_first=analog["p_sl_first"],
        p_neither_4h=analog["p_neither"],
        tp_time_p25_min=analog["tp_time_p25_min"],
        tp_time_median_min=analog["tp_time_median_min"],
        tp_time_p75_min=analog["tp_time_p75_min"],
        reasons_long=rl, reasons_short=rs, vetoes=veto, warnings=warnings
    )
