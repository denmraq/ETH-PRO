
from __future__ import annotations
import time
import threading
from state_store import save_flow_trades, flow_window as persistent_flow_window, kv_get, kv_set
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional
import requests
import pandas as pd
import numpy as np
from macro_news import snapshot as news_snapshot
from liquidation_engine import safe_snapshot as liquidation_snapshot, apply_to_forward as apply_liquidation_to_forward

BASE_URL = "https://api.bybit.com"
BINANCE_URL = "https://fapi.binance.com"
OKX_URL = "https://www.okx.com"
COINBASE_URL = "https://api.exchange.coinbase.com"
KRAKEN_URL = "https://api.kraken.com"
OKX_INST = "ETH-USDT-SWAP"
COINBASE_PRODUCT = "ETH-USD"
KRAKEN_PAIR = "ETHUSD"
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


def _kraken_get(path: str, params=None):
    r = SESSION.get(KRAKEN_URL + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError("Kraken API error: " + ", ".join(map(str, data.get("error") or [])))
    return data

def _kraken_interval(interval: str) -> int:
    return {"1m":1,"5m":5,"15m":15,"1h":60,"4h":240}[interval]

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
    # 4) Kraken spot. Public OHLC supports 1m/5m/15m/1h/4h directly and is
    # a strong fallback for Render regions where derivative APIs return 403/451.
    if df is None or not len(df):
        try:
            data=_kraken_get("/0/public/OHLC", {"pair":KRAKEN_PAIR,"interval":_kraken_interval(interval)})
            result=data.get("result",{})
            pair_key=next((k for k in result.keys() if k != "last"), None)
            rows=(result.get(pair_key) or [])[-min(int(limit),720):] if pair_key else []
            # Kraken: [time, open, high, low, close, vwap, volume, count]
            df=pd.DataFrame([[int(r[0])*1000,r[1],r[2],r[3],r[4],r[6],float(r[4])*float(r[6])] for r in rows],columns=["open_time","open","high","low","close","volume","quote_volume"])
        except Exception as e:
            errors.append(f"Kraken:{type(e).__name__}"); df=None
    # 5) Coinbase spot. Coinbase does not support native 4h candles, so for 4h
    # we fetch 1h candles and resample instead of making an invalid 14400s request.
    if df is None or not len(df):
        try:
            if interval != "4h":
                raw=_coinbase_get(f"/products/{COINBASE_PRODUCT}/candles",{"granularity":_coinbase_granularity(interval)})
                raw=raw[:min(int(limit),300)]
                df=pd.DataFrame([[int(r[0])*1000,r[3],r[2],r[1],r[4],r[5],float(r[4])*float(r[5])] for r in raw],columns=["open_time","open","high","low","close","volume","quote_volume"])
            else:
                raw=_coinbase_get(f"/products/{COINBASE_PRODUCT}/candles",{"granularity":3600})[:300]
                tmp=pd.DataFrame([[pd.to_datetime(int(r[0]),unit="s",utc=True),float(r[3]),float(r[2]),float(r[1]),float(r[4]),float(r[5])] for r in raw],columns=["dt","open","high","low","close","volume"]).sort_values("dt").set_index("dt")
                agg=tmp.resample("4h").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
                agg["quote_volume"]=agg["close"]*agg["volume"]
                agg=agg.tail(min(int(limit),len(agg))).reset_index()
                df=pd.DataFrame({"open_time":(agg["dt"].astype("int64")//1_000_000),"open":agg["open"],"high":agg["high"],"low":agg["low"],"close":agg["close"],"volume":agg["volume"],"quote_volume":agg["quote_volume"]})
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
        lambda: float(next(iter(_kraken_get("/0/public/Ticker", {"pair":KRAKEN_PAIR})["result"].values()))["c"][0]),
        lambda: float(_coinbase_get(f"/products/{COINBASE_PRODUCT}/ticker")["price"]),
    ):
        try: return fn()
        except Exception: pass
    raise RuntimeError("All live-price sources failed")

def order_book_imbalance(depth: int = 50):
    """Current L2 order-book imbalance, used only as a bounded forward feature.

    Returns (imbalance_pct, spread_bps, source). Positive means more bid notional
    near the market; negative means more ask notional. This is NOT a liquidation map.
    """
    # Bybit linear order book
    try:
        data=_get("/v5/market/orderbook",{"category":CATEGORY,"symbol":SYMBOL,"limit":min(max(depth,1),200)})
        book=data.get("result",{})
        bids=book.get("b",[])[:depth]; asks=book.get("a",[])[:depth]
        bn=sum(float(px)*float(sz) for px,sz,*_ in bids); an=sum(float(px)*float(sz) for px,sz,*_ in asks)
        if bn+an>0 and bids and asks:
            imb=(bn-an)/(bn+an)*100.0
            mid=(float(bids[0][0])+float(asks[0][0]))/2.0
            spread=(float(asks[0][0])-float(bids[0][0]))/mid*10000.0
            return float(imb),float(spread),'Bybit L2'
    except Exception:
        pass
    # OKX fallback
    try:
        data=_okx_get("/api/v5/market/books",{"instId":OKX_INST,"sz":min(max(depth,1),400)})
        book=data.get("data",[])[0]; bids=book.get("bids",[])[:depth]; asks=book.get("asks",[])[:depth]
        bn=sum(float(r[0])*float(r[1]) for r in bids); an=sum(float(r[0])*float(r[1]) for r in asks)
        if bn+an>0 and bids and asks:
            imb=(bn-an)/(bn+an)*100.0
            mid=(float(bids[0][0])+float(asks[0][0]))/2.0
            spread=(float(asks[0][0])-float(bids[0][0]))/mid*10000.0
            return float(imb),float(spread),'OKX L2'
    except Exception:
        pass
    return None,None,'Unavailable'

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

def current_open_interest_snapshot():
    """Fetch current OI from any venue that Render can reach.

    Historical endpoints are often geo-blocked on Render. A current snapshot is
    still public on OKX, Bybit ticker and Binance. We persist snapshots locally
    and build our own ~30m history instead of pretending OI is unavailable.
    """
    sources = (
        ("OKX", lambda: float(_okx_get("/api/v5/public/open-interest", {"instType":"SWAP","instId":OKX_INST})["data"][0]["oi"])),
        ("Bybit", lambda: float(_get("/v5/market/tickers", {"category":CATEGORY,"symbol":SYMBOL})["result"]["list"][0]["openInterest"])),
        ("Binance", lambda: float(_binance_get("/fapi/v1/openInterest", {"symbol":SYMBOL})["openInterest"])),
    )
    for name, fn in sources:
        try:
            v=fn()
            if np.isfinite(v) and v>0:
                return v, name
        except Exception:
            pass
    return None, "Unavailable"

def local_oi_change(minutes: int = 30):
    """Persist current OI snapshots and estimate change over the requested window.

    Returns (pct_change_or_None, source, coverage_minutes, count). The value is
    only emitted once there is enough elapsed time to make the comparison useful.
    """
    value, source = current_open_interest_snapshot()
    now=time.time()
    hist=kv_get("oi_snapshots_v1", []) or []
    clean=[]
    for row in hist:
        try:
            ts=float(row[0]); val=float(row[1]); src=str(row[2]) if len(row)>2 else ""
            if now-ts <= 3*3600 and np.isfinite(val) and val>0:
                clean.append([ts,val,src])
        except Exception:
            pass
    if value is not None:
        # Avoid duplicate points when the user taps refresh several times per minute.
        if not clean or now-clean[-1][0] >= 45:
            clean.append([now,float(value),source])
        else:
            clean[-1]=[now,float(value),source]
    try:
        kv_set("oi_snapshots_v1", clean[-240:])
    except Exception:
        pass
    if len(clean)<2:
        return None, source, 0.0, len(clean)
    latest=clean[-1]
    target_ts=latest[0]-minutes*60
    # Prefer the closest point to 30m ago; require at least 12m coverage so an
    # ultra-short sample is never mislabeled as a 30m OI change.
    same=[r for r in clean[:-1] if (not latest[2] or r[2]==latest[2])]
    if not same:
        return None, source, 0.0, len(clean)
    prior=min(same, key=lambda r: abs(r[0]-target_ts))
    coverage=(latest[0]-prior[0])/60.0
    if coverage < 12.0:
        return None, source, round(coverage,1), len(clean)
    ch=pct(float(latest[1]), float(prior[1]))
    return float(ch), source, round(coverage,1), len(clean)

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
    """Return a pullback entry band, not a chase/breakout band.

    User-facing rule:
      * LONG entry must be below (or just under) the current live price.
      * SHORT entry must be above (or just over) the current live price.

    EMA20 remains the structural anchor, but the band is clipped to the correct
    side of live price so the radar searches for a better re-entry instead of
    asking the user to buy higher in LONG or sell lower in SHORT.
    """
    atr = max(float(atr15), 1e-9)
    anchor = float(ema20_15m)
    live = float(price)
    gap = max(0.05 * atr, abs(live) * 0.00015)
    width = max(0.50 * atr, abs(live) * 0.0008)
    if direction == "LONG":
        # Prefer EMA pullback when EMA is already below price; otherwise place
        # the re-entry just under live price rather than above it.
        high = min(anchor + 0.10 * atr, live - gap)
        low = high - width
    elif direction == "SHORT":
        # Mirror image: wait for a bounce/retest above live price.
        low = max(anchor - 0.10 * atr, live + gap)
        high = low + width
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
        gap=max(0.05*atr5, abs(price)*0.00015)
        new_high=min(anchor+0.10*atr5, price-gap)
        new_low=new_high-max(0.50*atr5, abs(price)*0.0008)
        local_stop=rlo-0.18*atr5
        new_stop=min(local_stop, (new_low+new_high)/2-0.75*atr5)
        cands=[x for x in t if x > price+eps]
        cands += [float(x) for x in d1["high"].tail(120).values if float(x) > price+eps]
        base=max(price, rhi, fast_close)
        cands += [base+0.8*atr15, base+1.6*atr15, base+2.5*atr15, base+3.4*atr15]
        future=_pick_forward(cands, +1)
    else:
        gap=max(0.05*atr5, abs(price)*0.00015)
        new_low=max(anchor-0.10*atr5, price+gap)
        new_high=new_low+max(0.50*atr5, abs(price)*0.0008)
        local_stop=rhi+0.18*atr5
        new_stop=max(local_stop, (new_low+new_high)/2+0.75*atr5)
        cands=[x for x in t if x < price-eps]
        cands += [float(x) for x in d1["low"].tail(120).values if float(x) < price-eps]
        base=min(price, rlo, fast_close)
        cands += [base-0.8*atr15, base-1.6*atr15, base-2.5*atr15, base-3.4*atr15]
        future=_pick_forward(cands, -1)
    return {"stage":stage,"targets_hit":hit,"entry_low":min(new_low,new_high),"entry_high":max(new_low,new_high),
            "stop":new_stop,"targets":future[:3],"rollover":True}


def _softmax3(a: float, b: float, c: float):
    vals=np.array([a,b,c],dtype=float)
    vals=vals-np.max(vals)
    ex=np.exp(vals)
    p=ex/ex.sum()
    return [float(x) for x in p]

def _clip01(x: float) -> float:
    return float(max(0.0,min(1.0,x)))

def forward_outlook(price: float, direction: str, stop: float, target: float,
                    d1m: pd.DataFrame, d5: pd.DataFrame, d15: pd.DataFrame, d1: pd.DataFrame, d4: pd.DataFrame,
                    flow5: Optional[float], flow15: Optional[float], oi_change: float, funding_pct: float,
                    regime: str, simulations: int = 4000):
    """Forward probabilistic model from the CURRENT market state.

    V1.9 keeps the simulation internal and exposes only the most likely LONG/SHORT
    direction for 1h, 6h and 12h. No WAIT/RANGE label is emitted to the user.
    """
    def last(df): return df.iloc[-1]
    def slope_norm(df, lookback=4):
        z=last(df); atr=max(float(z['atr14']),1e-9)
        return float((z['ema20']-df.iloc[-lookback]['ema20'])/atr)
    def gap_norm(df):
        z=last(df); atr=max(float(z['atr14']),1e-9)
        return float((z['close']-z['ema20'])/atr)
    def ret_norm(df, n=4):
        z=last(df); atr=max(float(z['atr14']),1e-9)
        return float((z['close']-df.iloc[-1-n]['close'])/atr)

    # V1.17 anti-candle-follow layer. Direction must describe the *next* move,
    # not simply echo the colour of the latest candle. The live price has only
    # a small weight; persistence, slope and acceleration across several CLOSED
    # bars dominate the immediate horizon.
    def persistence(df, n=6):
        x=pd.to_numeric(df['close'],errors='coerce').diff().tail(n).dropna()
        if len(x)<3: return 0.0
        return float(np.clip(x.gt(0).mean()-x.lt(0).mean(),-1,1))
    def accel_norm(df):
        if len(df)<8: return 0.0
        atr=max(float(last(df)['atr14']),1e-9)
        fast=float(df['close'].iloc[-1]-df['close'].iloc[-4])/atr
        slow=float(df['close'].iloc[-4]-df['close'].iloc[-8])/atr
        return float(np.tanh(fast-slow))
    def wick_reversal(df):
        z=last(df); rng=max(float(z['high']-z['low']),1e-9)
        body=float(z['close']-z['open'])
        upper=float(z['high']-max(z['open'],z['close']))/rng
        lower=float(min(z['open'],z['close'])-z['low'])/rng
        # Positive means rejection of lower prices; negative means rejection of highs.
        return float(np.clip((lower-upper)*0.8 + np.sign(body)*min(abs(body)/rng,1)*0.2,-1,1))

    z1=last(d1m); atr1=max(float(z1['atr14']),1e-9)
    live_gap=float(np.clip((price-float(z1['close']))/atr1,-1.5,1.5))
    f1=np.tanh(0.12*live_gap + 0.20*gap_norm(d1m)+0.30*slope_norm(d1m)+0.20*ret_norm(d1m,5)
               +0.55*persistence(d1m,7)+0.20*accel_norm(d1m)+0.12*wick_reversal(d1m))
    f5=np.tanh(0.28*gap_norm(d5)+0.50*slope_norm(d5)+0.20*ret_norm(d5,4)
               +0.55*persistence(d5,6)+0.22*accel_norm(d5)+0.10*wick_reversal(d5))
    f15=np.tanh(0.30*gap_norm(d15)+0.58*slope_norm(d15)+0.18*ret_norm(d15,4)
                +0.45*persistence(d15,6)+0.18*accel_norm(d15))
    f1h=np.tanh(0.34*gap_norm(d1)+0.68*slope_norm(d1)+0.15*ret_norm(d1,4)+0.34*persistence(d1,5))
    f4h=np.tanh(0.30*gap_norm(d4)+0.72*slope_norm(d4)+0.12*ret_norm(d4,3)+0.28*persistence(d4,4))
    # Поток важен для скальпинга, но незрелое окно нельзя считать наравне с полноценным.
    # Вес фактического flow масштабируется по накопленному времени окна ниже в analyze().
    ff5=np.tanh(float(flow5 or 0.0)/18.0)
    ff15=np.tanh(float(flow15 or 0.0)/14.0)

    p5chg=float(d5.iloc[-1]['ret4']) if pd.notna(d5.iloc[-1]['ret4']) else 0.0
    if oi_change > 0.15:
        foi=np.sign(p5chg)*min(1.0,abs(oi_change)/1.2)
    elif oi_change < -0.20:
        foi=-0.20*np.sign(p5chg)
    else:
        foi=0.0
    ffund=-float(np.clip(funding_pct/0.05,-1,1))*0.15

    # Horizon pressure. For 1h, 5m/15m persistence + flow dominate; a single 1m
    # move cannot flip the model by itself. For 6h/12h higher timeframes dominate.
    edge1h=(0.06*f1+0.25*f5+0.27*f15+0.13*f1h+0.03*f4h+0.14*ff5+0.07*ff15+0.05*foi+0.08*ffund)
    edge6h=(0.02*f1+0.08*f5+0.20*f15+0.31*f1h+0.25*f4h+0.04*ff5+0.05*ff15+0.05*foi+0.12*ffund)
    edge12h=(0.01*f1+0.03*f5+0.11*f15+0.30*f1h+0.42*f4h+0.02*ff5+0.04*ff15+0.07*foi+0.14*ffund)
    edge1h=float(np.clip(edge1h,-1.5,1.5)); edge6h=float(np.clip(edge6h,-1.5,1.5)); edge12h=float(np.clip(edge12h,-1.5,1.5))

    atr15=max(float(d15.iloc[-1]['atr14']),1e-9)
    rets=pd.to_numeric(d1m['close'],errors='coerce').pct_change().dropna().tail(180)
    sigma1=float(rets.std(ddof=0)) if len(rets)>=20 else 0.0
    atr_floor=max(float(d1m.iloc[-1]['atr14'])/price/2.5, atr15/price/18.0, 1e-5)
    sigma1=max(sigma1,atr_floor)

    ts=int(pd.Timestamp(d1m.iloc[-1]['close_time']).timestamp()//60)
    seed=(ts*1315423911 + int(round(price*100)) + int(round(edge1h*10000))) & 0xffffffff
    rng=np.random.default_rng(seed)
    steps=720  # 12 hours at 1-minute resolution

    # Drift evolves from the fast 1h state toward slower 6h and 12h structure.
    edge_curve=np.empty(steps,dtype=float)
    edge_curve[:360]=np.linspace(edge1h,edge6h,360)
    edge_curve[360:]=np.linspace(edge6h,edge12h,360)
    drift_curve=np.clip(edge_curve*sigma1*0.16,-sigma1*0.22,sigma1*0.22)
    shocks=rng.normal(loc=0.0,scale=sigma1,size=(int(simulations),steps))
    shocks += drift_curve
    paths=price*np.exp(np.cumsum(shocks,axis=1))

    # Trade-path statistics remain available internally for TP/SL blocks.
    trade_horizon=240  # keep TP/SL transaction statistics on the original 4h horizon
    trade_paths=paths[:,:trade_horizon]
    if direction=='LONG':
        hit_tp=trade_paths>=float(target); hit_sl=trade_paths<=float(stop)
    else:
        hit_tp=trade_paths<=float(target); hit_sl=trade_paths>=float(stop)
    has_tp=hit_tp.any(axis=1); has_sl=hit_sl.any(axis=1)
    first_tp=np.where(has_tp,hit_tp.argmax(axis=1)+1,trade_horizon+1)
    first_sl=np.where(has_sl,hit_sl.argmax(axis=1)+1,trade_horizon+1)
    tp_first=first_tp<first_sl
    sl_first=(first_sl<=first_tp) & (~((~has_tp)&(~has_sl)))
    neither=(~has_tp)&(~has_sl)
    tp_times=first_tp[tp_first]
    def qmin(vals,p):
        return int(round(float(np.quantile(vals,p))/5)*5) if len(vals) else None

    # V1.15: дальний горизонт не имеет права становиться «увереннее» просто
    # потому, что старшие EMA смотрят в одну сторону. Чем дальше горизонт,
    # тем сильнее штраф за неопределённость. Высокая вероятность сохраняется
    # только при согласованности нескольких независимых слоёв.
    coherence=float(np.mean(np.sign([f5,f15,f1h,f4h]) == np.sign(edge1h)))
    coherence=float(np.clip(coherence,0.0,1.0))

    def horizon(idx, shrink, hard_cap):
        vals=paths[:,idx-1]
        up=float((vals>price).mean()); down=float((vals<=price).mean())
        long_p=up*100.0
        side='LONG' if long_p>=50.0 else 'SHORT'
        raw=max(long_p,100.0-long_p)
        # shrink raw edge toward 50%; a coherent market earns part of it back.
        eff=shrink*(0.72+0.28*coherence)
        prob=50.0+(raw-50.0)*eff
        prob=min(prob, hard_cap)
        q10,q50,q90=[float(x) for x in np.quantile(vals,[.10,.50,.90])]
        return side, round(prob,1), round(q10,2), round(q50,2), round(q90,2), up, down

    h1=horizon(60,0.92,82.0)
    h6=horizon(360,0.62,74.0)
    h12=horizon(720,0.48,70.0)

    signs=np.array([f1,f5,f15,f1h,f4h,ff5,ff15],dtype=float)
    agreement=abs(float(signs.mean()))
    separation=abs(h1[5]-h1[6])
    confidence=int(round(np.clip(45+30*agreement+25*separation,35,95)))

    return {
        'direction_1h':h1[0], 'probability_1h':h1[1],
        'expected_1h_low':h1[2], 'expected_1h_mid':h1[3], 'expected_1h_high':h1[4],
        'direction_6h':h6[0], 'probability_6h':h6[1],
        'expected_6h_low':h6[2], 'expected_6h_mid':h6[3], 'expected_6h_high':h6[4],
        'direction_12h':h12[0], 'probability_12h':h12[1],
        'expected_12h_low':h12[2], 'expected_12h_mid':h12[3], 'expected_12h_high':h12[4],
        'forecast_confidence':confidence,
        'path_count':int(simulations),
        'p_tp_first':round(float(tp_first.mean())*100,1),
        'p_sl_first':round(float(sl_first.mean())*100,1),
        'p_neither':round(float(neither.mean())*100,1),
        'tp_time_p25_min':qmin(tp_times,.25),'tp_time_median_min':qmin(tp_times,.50),'tp_time_p75_min':qmin(tp_times,.75),
        'first_event_median_min':qmin(np.minimum(first_tp,first_sl)[np.minimum(first_tp,first_sl)<=trade_horizon],.50),
        # Compatibility fields for older clients; no longer shown in V1.9 UI.
        'up_15m':round(h1[5]*100,1),'range_15m':0.0,'down_15m':round(h1[6]*100,1),
        'up_60m':round(h1[5]*100,1),'range_60m':0.0,'down_60m':round(h1[6]*100,1),
        'expected_60m_low':h1[2],'expected_60m_mid':h1[3],'expected_60m_high':h1[4],
        'breakout_up_level':h1[4],'breakdown_level':h1[2],
        'p_breakout_up_60m':0.0,'p_breakdown_60m':0.0,'p_target_60m':0.0,
        'momentum_delta':round((edge1h-edge12h)*100,1),
        'edge_15m':round(edge1h,4),'edge_60m':round(edge1h,4),
    }




# ---------------- V1.19 FUTURE ANALOG PREDICTOR ----------------
# This engine is deliberately supervised on FUTURE outcomes.  It does not ask
# "what colour is the last candle?".  It asks: when the market previously had
# a state similar to the current one, where was ETH 1h / 6h / 12h later?
_H5_CACHE = {"ts": 0.0, "df": None}

def _history_5m_deep(target: int = 5000) -> pd.DataFrame:
    """Fetch enough CLOSED 5m history to label future outcomes. Cached 15m.

    Bybit is preferred because the app already uses its perpetual contract.  If
    Render cannot reach historical pages, fall back to the ordinary multi-source
    candle loader.  The predictor still runs, but reports lower confidence.
    """
    now=time.time()
    cached=_H5_CACHE.get("df")
    if isinstance(cached,pd.DataFrame) and len(cached)>=900 and now-float(_H5_CACHE.get("ts",0))<900:
        return cached.copy()
    rows=[]; end=None
    try:
        while len(rows)<target:
            params={"category":CATEGORY,"symbol":SYMBOL,"interval":"5","limit":min(1000,target-len(rows))}
            if end is not None: params["end"]=int(end)
            data=_get("/v5/market/kline",params)
            batch=data.get("result",{}).get("list",[])
            if not batch: break
            rows.extend(batch)
            oldest=min(int(float(r[0])) for r in batch)
            end=oldest-1
            if len(batch)<params["limit"]: break
            time.sleep(0.03)
        if rows:
            df=pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","quote_volume"])
            for c in ["open","high","low","close","volume","quote_volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
            df["open_time"]=pd.to_datetime(pd.to_numeric(df["open_time"]),unit="ms",utc=True)
            df=df.dropna(subset=["open_time","open","high","low","close"]).sort_values("open_time").drop_duplicates("open_time")
            df["close_time"]=df["open_time"]+pd.Timedelta(minutes=5)
            df=df[df["close_time"]<=pd.Timestamp.now(tz="UTC")].reset_index(drop=True)
        else: df=pd.DataFrame()
    except Exception:
        df=pd.DataFrame()
    if len(df)<900:
        try: df=klines("5m",1000,True)
        except Exception: pass
    _H5_CACHE["ts"]=now; _H5_CACHE["df"]=df.copy()
    return df.copy()

def _future_feature_frame(raw: pd.DataFrame) -> pd.DataFrame:
    x=indicators(raw).copy()
    c=x['close'].astype(float); atr=x['atr14'].replace(0,np.nan).astype(float)
    r=c.pct_change()
    for n in (1,3,6,12,24,48): x[f'f_ret{n}']=c.pct_change(n)
    x['f_ema20_gap']=(c-x['ema20'])/atr
    x['f_ema50_gap']=(c-x['ema50'])/atr
    x['f_ema20_slope']=(x['ema20']-x['ema20'].shift(6))/atr
    x['f_ema50_slope']=(x['ema50']-x['ema50'].shift(12))/atr
    x['f_rsi']=(x['rsi14']-50.0)/25.0
    x['f_atr_pct']=x['atr14']/c
    body=(x['close']-x['open'])/atr
    rng=(x['high']-x['low']).replace(0,np.nan)
    x['f_body']=body
    x['f_upper']=(x['high']-x[['open','close']].max(axis=1))/rng
    x['f_lower']=(x[['open','close']].min(axis=1)-x['low'])/rng
    v=x['quote_volume'].replace(0,np.nan).astype(float)
    lv=np.log(v)
    x['f_vol_z']=(lv-lv.rolling(48).mean())/lv.rolling(48).std(ddof=0).replace(0,np.nan)
    abs_move=c.diff().abs().rolling(12).sum().replace(0,np.nan)
    x['f_eff12']=(c-c.shift(12))/abs_move
    x['f_accel']=x['f_ret3']-(x['f_ret12']/4.0)
    hi=x['high'].rolling(24).max(); lo=x['low'].rolling(24).min(); span=(hi-lo).replace(0,np.nan)
    x['f_pos24']=(c-lo)/span*2.0-1.0
    return x

_FUTURE_FEATURES=[
    'f_ret1','f_ret3','f_ret6','f_ret12','f_ret24','f_ret48',
    'f_ema20_gap','f_ema50_gap','f_ema20_slope','f_ema50_slope','f_rsi',
    'f_atr_pct','f_body','f_upper','f_lower','f_vol_z','f_eff12','f_accel','f_pos24'
]

def _weighted_quantile(values, weights, qs):
    values=np.asarray(values,float); weights=np.asarray(weights,float)
    order=np.argsort(values); values=values[order]; weights=weights[order]
    cw=np.cumsum(weights); total=float(cw[-1]) if len(cw) else 0.0
    if total<=0: return [float(np.nanquantile(values,q)) for q in qs]
    return [float(np.interp(q*total,cw,values)) for q in qs]


def _weighted_median(values, weights):
    if not values:
        return None
    vals=np.asarray(values,float); w=np.asarray(weights,float)
    order=np.argsort(vals); vals=vals[order]; w=w[order]
    c=np.cumsum(w); total=float(c[-1]) if len(c) else 0.0
    if total<=0: return int(round(float(np.median(vals))))
    return int(round(float(vals[np.searchsorted(c, total*0.5)])))

def _analog_horizon(feat: pd.DataFrame, horizon_bars: int, price: float, k: int = 100):
    """Future-event analogue model.

    Historical states are matched to the CURRENT state, but labels come from bars
    that happened AFTER each state. The target is which directional excursion
    was reached first inside the horizon, plus where price finished later.
    """
    cur=feat.iloc[-1]
    hist=feat.iloc[:-horizon_bars].copy()
    hist['future_ret']=feat['close'].shift(-horizon_bars).iloc[:-horizon_bars].values / hist['close'].values - 1.0
    cols=_FUTURE_FEATURES
    hist=hist.dropna(subset=cols+['future_ret'])
    if len(hist)<160:
        return None
    if len(hist)>horizon_bars+8:
        hist=hist.iloc[:-max(3,horizon_bars//3)]
    X=hist[cols].astype(float).to_numpy(); q=cur[cols].astype(float).to_numpy()
    med=np.nanmedian(X,axis=0); scale=np.nanmedian(np.abs(X-med),axis=0)*1.4826
    scale=np.where((~np.isfinite(scale)) | (scale<1e-8), np.nanstd(X,axis=0), scale)
    scale=np.where((~np.isfinite(scale)) | (scale<1e-8), 1.0, scale)
    Xz=np.clip((X-med)/scale,-6,6); qz=np.clip((q-med)/scale,-6,6)
    fw=np.array([0.15,0.35,0.65,0.95,0.90,0.70,1.0,0.75,1.25,0.95,0.85,0.90,0.15,0.65,0.65,0.75,1.15,1.30,1.05])
    d=np.sqrt(np.nanmean(((Xz-qz)*fw)**2,axis=1))
    kk=min(k,len(d)); idx=np.argpartition(d,kk-1)[:kk]
    ds=d[idx]; selected=hist.iloc[idx]
    y=selected['future_ret'].to_numpy()
    tau=max(float(np.median(ds)),0.20)
    w=np.exp(-0.5*(ds/tau)**2)
    ages=(len(hist)-idx)/max(len(hist),1)
    w*=np.exp(-0.30*ages)

    event=[]; event_minutes=[]; terminal=[]
    for row_i, (_, row) in enumerate(selected.iterrows()):
        loc=feat.index.get_loc(row.name)
        future=feat.iloc[loc+1:loc+1+horizon_bars]
        base=float(row['close']); atr=max(float(row['atr14']), base*0.0005)
        barrier=max(atr*(max(horizon_bars,1)**0.5)*0.65, base*0.0015)
        up=base+barrier; down=base-barrier
        side=0; mins=horizon_bars*5
        for j, fr in enumerate(future.itertuples(), start=1):
            hit_up=float(fr.high)>=up; hit_dn=float(fr.low)<=down
            if hit_up and hit_dn:
                side=1 if float(fr.close)>=base else -1; mins=j*5; break
            if hit_up: side=1; mins=j*5; break
            if hit_dn: side=-1; mins=j*5; break
        event.append(side); event_minutes.append(mins); terminal.append(float(y[row_i]))
    event=np.asarray(event,int); terminal=np.asarray(terminal,float)

    first_long=float(np.sum(w*(event==1))/max(np.sum(w),1e-12))
    first_short=float(np.sum(w*(event==-1))/max(np.sum(w),1e-12))
    term_long=float(np.sum(w*(terminal>0))/max(np.sum(w),1e-12))
    unresolved=max(0.0,1.0-first_long-first_short)
    long_p=0.72*first_long + 0.28*term_long + 0.50*unresolved*0.28
    short_p=1.0-long_p
    side='LONG' if long_p>=short_p else 'SHORT'
    raw=max(long_p,short_p)*100.0
    eff_n=float((w.sum()**2)/max(np.sum(w*w),1e-12))
    reliability=np.clip((eff_n-14.0)/55.0,0.30,1.0)
    prob=50.0+(raw-50.0)*reliability
    q10,q50,q90=_weighted_quantile(y,w,[.10,.50,.90])
    chosen=1 if side=='LONG' else -1
    tvals=[event_minutes[i] for i in range(len(event)) if event[i]==chosen]
    tw=[w[i] for i in range(len(event)) if event[i]==chosen]
    eta=_weighted_median(tvals,tw)
    return {
        'side':side,'prob':float(prob),'up':long_p,'down':short_p,
        'low':price*(1.0+q10),'mid':price*(1.0+q50),'high':price*(1.0+q90),
        'support':int(kk),'effective_n':round(eff_n,1),'median_return':float(q50),
        'raw_prob':float(raw),'eta_min':eta,
        'first_touch_long':round(first_long*100,1),'first_touch_short':round(first_short*100,1),
    }

def future_analog_outlook(price: float, flow5: Optional[float], flow15: Optional[float],
                          oi_change: float, funding_pct: float, lob_imbalance: Optional[float]=None, simulations: int = 0):
    """Predict the next directional leg from future-labelled historical analogues."""
    raw=_history_5m_deep(7000)
    feat=_future_feature_frame(raw)
    h1=_analog_horizon(feat,12,price,120)
    h6=_analog_horizon(feat,72,price,150)
    h12=_analog_horizon(feat,144,price,180)
    if not all((h1,h6,h12)):
        raise RuntimeError('Недостаточно истории для модели будущего движения')

    flow_edge=0.0
    if flow5 is not None: flow_edge += 0.45*np.tanh(float(flow5)/24.0)
    if flow15 is not None: flow_edge += 0.35*np.tanh(float(flow15)/20.0)
    if oi_change is not None and abs(float(oi_change))>0.05:
        flow_edge += 0.15*np.tanh(float(oi_change)/0.8)
    flow_edge += -0.05*np.tanh(float(funding_pct)/0.03)
    if lob_imbalance is not None:
        flow_edge += 0.30*np.tanh(float(lob_imbalance)/22.0)

    def adjust(h, strength, cap):
        signed=(h['prob']-50.0)*(1 if h['side']=='LONG' else -1)
        candidate=signed + strength*flow_edge
        if np.sign(candidate)!=np.sign(signed) and abs(candidate)<10.0:
            candidate=np.sign(signed)*max(0.5,abs(signed)-1.0)
        side='LONG' if candidate>=0 else 'SHORT'
        out=dict(h); out['side']=side; out['prob']=min(cap,50.0+abs(candidate))
        return out
    h1=adjust(h1,4.0,79.0); h6=adjust(h6,2.0,76.0); h12=adjust(h12,1.0,74.0)

    signs=[1 if h['side']=='LONG' else -1 for h in (h1,h6,h12)]
    agreement=abs(sum(signs))/3.0
    support=min(h1['effective_n'],h6['effective_n'],h12['effective_n'])
    confidence=int(np.clip(42+22*agreement+min(28,support/2.0),42,92))
    return {
      'direction_1h':h1['side'],'probability_1h':round(h1['prob'],1),'eta_1h_min':h1['eta_min'],
      'expected_1h_low':round(h1['low'],2),'expected_1h_mid':round(h1['mid'],2),'expected_1h_high':round(h1['high'],2),
      'direction_6h':h6['side'],'probability_6h':round(h6['prob'],1),'eta_6h_min':h6['eta_min'],
      'expected_6h_low':round(h6['low'],2),'expected_6h_mid':round(h6['mid'],2),'expected_6h_high':round(h6['high'],2),
      'direction_12h':h12['side'],'probability_12h':round(h12['prob'],1),'eta_12h_min':h12['eta_min'],
      'expected_12h_low':round(h12['low'],2),'expected_12h_mid':round(h12['mid'],2),'expected_12h_high':round(h12['high'],2),
      'forecast_confidence':confidence,
      'path_count':int(h1['support']+h6['support']+h12['support']),
      'analog_support_1h':h1['support'],'analog_support_6h':h6['support'],'analog_support_12h':h12['support'],
      'p_tp_first':None,'p_sl_first':None,'p_neither':None,
      'tp_time_p25_min':None,'tp_time_median_min':None,'tp_time_p75_min':None,'first_event_median_min':h1['eta_min'],
      'up_15m':round(h1['up']*100,1),'range_15m':0.0,'down_15m':round(h1['down']*100,1),
      'up_60m':round(h1['up']*100,1),'range_60m':0.0,'down_60m':round(h1['down']*100,1),
      'expected_60m_low':round(h1['low'],2),'expected_60m_mid':round(h1['mid'],2),'expected_60m_high':round(h1['high'],2),
      'breakout_up_level':round(h1['high'],2),'breakdown_level':round(h1['low'],2),
      'p_breakout_up_60m':0.0,'p_breakdown_60m':0.0,'p_target_60m':0.0,
      'momentum_delta':round((h1['median_return']-h12['median_return'])*100,2),
    }

def stabilize_future_forecast(fwd: dict, force_refresh: bool=False):
    """Clock-based forecasts with hysteresis."""
    now=time.time()
    configs={'1h':(1200,60.0,8.0),'6h':(3600,59.0,7.0),'12h':(7200,58.0,6.0)}
    for h,(ttl,flip_min,margin) in configs.items():
        key=f'future_predict_lock_{h}_v120'; dkey=f'direction_{h}'; pkey=f'probability_{h}'
        try: st=kv_get(key,{}) or {}
        except Exception: st={}
        cand_d=str(fwd[dkey]); cand_p=float(fwd[pkey])
        if (not force_refresh) and st.get('direction') in ('LONG','SHORT') and now-float(st.get('ts',0))<ttl:
            fwd[dkey]=st['direction']; fwd[pkey]=float(st['probability'])
            eta_key=f'eta_{h}_min'
            if eta_key in st: fwd[eta_key]=st[eta_key]
            continue
        prev_d=str(st.get('direction') or ''); prev_p=float(st.get('probability') or 50.0)
        out_d,out_p=cand_d,cand_p
        if prev_d in ('LONG','SHORT') and cand_d!=prev_d and not force_refresh:
            if cand_p<flip_min or (cand_p-50.0) < max(0.0,(prev_p-50.0)-margin):
                out_d=prev_d; out_p=max(50.1,prev_p-1.5)
        state={'direction':out_d,'probability':round(out_p,1),'ts':now,f'eta_{h}_min':fwd.get(f'eta_{h}_min')}
        try: kv_set(key,state)
        except Exception: pass
        fwd[dkey]=out_d; fwd[pkey]=round(out_p,1)
    return fwd

# ---------------- END V1.20 FUTURE ENGINE ----------------


def current_price_levels(price: float, side: str, fwd: dict, atr15: float,
                         d1m: Optional[pd.DataFrame]=None, d5: Optional[pd.DataFrame]=None, d15: Optional[pd.DataFrame]=None):
    """Scalp TP/SL from current price, independent of 6h/12h horizons.

    The old implementation could select a tiny nearby wick as TP. For a scalp
    radar that is noise, not a useful target. V1.16 therefore uses the 1h
    forward range + 15m volatility, with sane ETH scalp floors and a minimum
    reward/risk. 1m/5m/15m still inform direction, but do not create a $3-$5 TP.
    """
    price=float(price); atr15=max(float(atr15),1e-9)
    atr5=max(float(d5.iloc[-1]['atr14']),1e-9) if d5 is not None and len(d5) else max(atr15/3.0,1e-9)

    # User-facing scalp geometry: normally ~15-35 USDT, widened only when
    # current 15m volatility genuinely warrants it. Never uses 6h/12h targets.
    min_tp=15.0
    vol_tp=float(np.clip(0.80*atr15, min_tp, 30.0))
    if side=='LONG':
        forward=max(float(fwd.get('expected_1h_high',price))-price,0.0)
    else:
        forward=max(price-float(fwd.get('expected_1h_low',price)),0.0)
    # Do not pretend the model has a large target if its 1h distribution is tight,
    # but also do not publish micro-noise as a take-profit.
    target_move=max(min_tp, min(35.0, max(vol_tp, 0.45*forward)))

    # Structural adverse level is advisory, then constrained to sane scalp risk.
    highs=[]; lows=[]
    for df,n in ((d5,36),(d15,24)):
        if df is None or not len(df): continue
        x=df.tail(n)
        highs.extend(float(v) for v in x['high'].iloc[:-1] if pd.notna(v))
        lows.extend(float(v) for v in x['low'].iloc[:-1] if pd.notna(v))

    min_risk=max(8.0, min(13.0, 0.35*atr15))
    # If volatility requires a wider valid stop, widen TP rather than publish bad R/R.
    target_move=max(target_move, 1.50*min_risk)
    max_risk=max(min_risk, min(18.0, target_move/1.50))  # reward/risk >= 1.50

    if side=='LONG':
        adverse=[v for v in lows if v<price-0.10*atr5]
        structural=(price-max(adverse)) + 0.15*atr5 if adverse else 0.70*atr15
        risk=float(np.clip(structural,min_risk,max_risk))
        return price, price-risk, price+target_move

    adverse=[v for v in highs if v>price+0.10*atr5]
    structural=(min(adverse)-price) + 0.15*atr5 if adverse else 0.70*atr15
    risk=float(np.clip(structural,min_risk,max_risk))
    return price, price+risk, price-target_move


def apply_news_to_forward(fwd: dict, news: dict | None):
    """Use news only when it has evidence of affecting the forward distribution."""
    if not news or not news.get('focus'):
        return fwd, {'used':False,'force_refresh':False,'delta_1h':0.0,'reason':'Новостного фактора для прогноза сейчас нет'}
    focus=news['focus']
    if not bool(focus.get('forecast_relevant')):
        return fwd, {'used':False,'force_refresh':False,'delta_1h':0.0,'reason':'Новости есть, но подтверждённого влияния на прогноз нет'}
    score=float(focus.get('score') or 0.0)
    impact=str(focus.get('impact') or 'LOW').upper()
    confirm=str(focus.get('market_confirms') or 'WAITING').upper()
    hist=float(focus.get('historical_reactivity') or 0.0)
    impact_w={'LOW':0.0,'MEDIUM':0.55,'HIGH':1.0}.get(impact,0.0)
    confirm_w={'WAITING':0.25,'NO':0.0,'YES':1.0}.get(confirm,0.0)
    evidence=np.clip(max(abs(score)/100.0, min(1.0,hist/0.5)),0.0,1.0)
    strength=np.sign(score)*(evidence*impact_w*confirm_w)
    out=dict(fwd); deltas={}
    for h,maxs in {'1h':6.0,'6h':3.5,'12h':2.5}.items():
        dkey=f'direction_{h}'; pkey=f'probability_{h}'
        d=str(out[dkey]); p=float(out[pkey]); long_p=p if d=='LONG' else 100.0-p
        delta=maxs*strength
        long_p=float(np.clip(long_p+delta,10.0,90.0))
        out[dkey]='LONG' if long_p>=50 else 'SHORT'
        out[pkey]=round(max(long_p,100.0-long_p),1); deltas[h]=round(delta,1)
    return out, {
        'used':True,'force_refresh':True,'event_id':focus.get('event_id'),'delta_1h':deltas['1h'],
        'reason':f"Новостной пересмотр: {focus.get('category_ru','событие')}, {impact}; вклад в 1ч {deltas['1h']:+.1f} п.п."
    }

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
    forecast_direction_1h: str
    forecast_probability_1h: float
    forecast_direction_6h: str
    forecast_probability_6h: float
    forecast_direction_12h: str
    forecast_probability_12h: float
    forecast_eta_1h_min: Optional[int]
    forecast_eta_6h_min: Optional[int]
    forecast_eta_12h_min: Optional[int]
    forecast_up_15m: float
    forecast_range_15m: float
    forecast_down_15m: float
    forecast_up_60m: float
    forecast_range_60m: float
    forecast_down_60m: float
    expected_60m_low: float
    expected_60m_mid: float
    expected_60m_high: float
    breakout_up_level: float
    breakdown_level: float
    p_breakout_up_60m: float
    p_breakdown_60m: float
    momentum_delta: float
    forecast_confidence: int
    forward_path_count: int
    trade_signal: str
    trade_signal_reason: str
    p_tp2_60m: float
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
    oi_change_pct: Optional[float]
    oi_source: str
    oi_coverage_min: float
    liquidation_available: bool
    liquidation_provider: str
    liquidation_mode: str
    liquidation_bias: str
    liquidation_dominance: float
    liquidation_nearest_above: Optional[float]
    liquidation_nearest_below: Optional[float]
    liquidation_strongest_above: Optional[float]
    liquidation_strongest_below: Optional[float]
    liquidation_adjustment_1h_pp: float
    liquidation_levels_used: int
    liquidation_note: str
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
    for df,label,pts in [(d4,"4ч",10),(d1,"1ч",10)]:
        z=df.iloc[-1]
        if z["close"] > z["ema20"] > z["ema50"] and z["ema20"] > df.iloc[-4]["ema20"]:
            ls += pts; rl.append(f"{label}: бычья структура EMA (+{pts})")
        elif z["close"] < z["ema20"] < z["ema50"] and z["ema20"] < df.iloc[-4]["ema20"]:
            ss += pts; rs.append(f"{label}: медвежья структура EMA (+{pts})")

    # 2) Closed 15m breakout only. Avoid current unfinished candle.
    z15=d15.iloc[-1]
    breakout_depth = 0.15 * float(z15["atr14"])
    if z15["close"] > z15["prior20_high"] + breakout_depth:
        ls += 5; rl.append("15м: подтверждённое закрытие выше предыдущего диапазона (+5)")
    elif z15["close"] < z15["prior20_low"] - breakout_depth:
        ss += 5; rs.append("15м: подтверждённое закрытие ниже предыдущего диапазона (+5)")

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
            ls += 8; rl.append(f"Поток 5м: агрессивные покупки, дельта +{d5flow:.1f}% (+8)")
        elif d5flow < -6:
            ss += 8; rs.append(f"Поток 5м: агрессивные продажи, дельта {d5flow:.1f}% (+8)")
    else:
        warnings.append(f"Поток 5м прогревается ({f5['coverage_min']:.1f}/4.0 мин нужно для включения в расчёт).")

    if d15flow is not None and f15["coverage_min"] >= 12.0:
        if d15flow > 4:
            ls += 7; rl.append(f"Поток 15м подтверждает покупки +{d15flow:.1f}% (+7)")
        elif d15flow < -4:
            ss += 7; rs.append(f"Поток 15м подтверждает продажи {d15flow:.1f}% (+7)")
    else:
        warnings.append(f"Поток 15м прогревается ({f15['coverage_min']:.1f}/12.0 мин нужно для включения в расчёт).")

    # 4) OI. Prefer exchange history; when Render blocks it, build our own
    # rolling history from public current-OI snapshots (OKX/Bybit/Binance).
    oi_ch=None; oi_source="Unavailable"; oi_coverage=0.0
    oih=open_interest_hist("5m",30)
    if len(oih)>=7:
        oi_ch=pct(float(oih.iloc[-1]["sumOpenInterest"]),float(oih.iloc[-7]["sumOpenInterest"]))
        oi_source="exchange history"; oi_coverage=30.0
    else:
        oi_ch, oi_source, oi_coverage, _oi_n = local_oi_change(30)
    p_ch=pct(float(d5.iloc[-1]["close"]),float(d5.iloc[-7]["close"]))
    if oi_ch is not None:
        if oi_ch>.20 and p_ch>.10:
            pts=min(15,8+int(min(7,oi_ch*3))); ls+=pts
            rl.append(f"OI +{oi_ch:.2f}% при устойчивом росте цены (+{pts})")
        elif oi_ch>.20 and p_ch<-.10:
            pts=min(15,8+int(min(7,oi_ch*3))); ss+=pts
            rs.append(f"OI +{oi_ch:.2f}% при устойчивом снижении цены (+{pts})")
        elif oi_ch<-.25:
            warnings.append("OI снижается: движение может идти за счёт закрытия позиций, а не новых входов.")
    else:
        warnings.append(f"OI: текущий снимок доступен ({oi_source}), история ~30м набирается {oi_coverage:.0f}/30 мин.")

    # 5) Nearby swing is context only, lower weight than old V0.2.
    swing_hi=float(d1["high"].iloc[-25:-1].max())
    swing_lo=float(d1["low"].iloc[-25:-1].min())
    up=(swing_hi/price-1)*100
    dn=(1-swing_lo/price)*100
    if 0<up<dn and up<1.0 and ls>=ss:
        ls+=5; rl.append(f"Ближайший верхний экстремум 1ч {swing_hi:.2f} (+5 context)")
    elif 0<dn<up and dn<1.0 and ss>=ls:
        ss+=5; rs.append(f"Ближайший нижний экстремум 1ч {swing_lo:.2f} (+5 context)")

    # 6) Momentum
    for df,label,pts in [(d15,"15м",6),(d5,"5м",4)]:
        z=df.iloc[-1]
        if z["close"]>z["ema20"] and z["ema20"]>df.iloc[-4]["ema20"]:
            ls+=pts; rl.append(f"{label}: импульс выше растущей EMA20 (+{pts})")
        elif z["close"]<z["ema20"] and z["ema20"]<df.iloc[-4]["ema20"]:
            ss+=pts; rs.append(f"{label}: импульс ниже падающей EMA20 (+{pts})")

    # 7) Funding
    funding_info=premium_index()
    funding=float(funding_info.get("lastFundingRate",0))*100
    if funding>.03 and ss>=ls:
        ss+=5; rs.append(f"Высокий положительный фандинг {funding:.4f}% (+5 SHORT)")
    elif funding<-.03 and ls>=ss:
        ls+=5; rl.append(f"Отрицательный фандинг {funding:.4f}% (+5 LONG)")

    # 8) RSI contextual
    r1=float(d1.iloc[-1]["rsi14"]); r15=float(d15.iloc[-1]["rsi14"])
    if 45<=r1<=68 and r15>52 and ls>ss:
        ls+=5; rl.append(f"RSI подтверждает LONG: 1H {r1:.1f}, 15m {r15:.1f} (+5)")
    elif 32<=r1<=55 and r15<48 and ss>ls:
        ss+=5; rs.append(f"RSI подтверждает SHORT: 1H {r1:.1f}, 15m {r15:.1f} (+5)")

    # 9) Closed-bar retest proxy
    atr15=float(d15.iloc[-1]["atr14"])
    prev=d15.iloc[-2]; cur=d15.iloc[-1]
    if abs(prev["low"]-prev["ema20"])<=.35*atr15 and cur["close"]>cur["ema20"] and cur["close"]>prev["close"]:
        ls+=10; rl.append("15м: ретест EMA20 удержан (+10)")
    elif abs(prev["high"]-prev["ema20"])<=.35*atr15 and cur["close"]<cur["ema20"] and cur["close"]<prev["close"]:
        ss+=10; rs.append("15м: отбой от EMA20 / ретест вниз (+10)")

    # 10) Fast 1m sensitivity layer. Small weight, fast reaction; higher TFs still dominate.
    z1m=d1m.iloc[-1]
    fast_bias="NEUTRAL"
    one_min_gap=(float(z1m["close"])-float(z1m["ema20"]))/max(float(z1m["atr14"]),1e-9)
    if z1m["close"]>z1m["ema20"] and z1m["ema20"]>d1m.iloc[-4]["ema20"]:
        ls+=4; fast_bias="LONG"; rl.append("1м: быстрый импульс выше растущей EMA20 (+4)")
    elif z1m["close"]<z1m["ema20"] and z1m["ema20"]<d1m.iloc[-4]["ema20"]:
        ss+=4; fast_bias="SHORT"; rs.append("1м: быстрый импульс ниже падающей EMA20 (+4)")
    # Extra 2 points only for an actual closed 1m range break, limiting noise.
    if z1m["close"] > z1m["prior20_high"] + 0.08*float(z1m["atr14"]):
        ls+=2; rl.append("1м: закрытый микропробой вверх (+2)")
    elif z1m["close"] < z1m["prior20_low"] - 0.08*float(z1m["atr14"]):
        ss+=2; rs.append("1м: закрытый микропробой вниз (+2)")

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
    if r15>76: veto.append("БЛОК LONG: RSI 15м в перекупленности.")
    if r15<24: veto.append("БЛОК SHORT: RSI 15м в перепроданности.")
    if price>cur["ema20"] and live_dist_atr>1.6:
        veto.append(f"БЛОК LONG: цена на {live_dist_atr:.1f} ATR выше EMA20 15м; не догонять движение.")
    if price<cur["ema20"] and live_dist_atr>1.6:
        veto.append(f"БЛОК SHORT: цена на {live_dist_atr:.1f} ATR ниже EMA20 15м; не догонять движение.")

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
                veto.append("LONG растянут: не догонять движение вверх.")
        else:
            structural_stop = rh + .15 * atr15
            atr_stop = entry_mid + .90 * atr15
            stop = max(structural_stop, atr_stop)
            stop = min(stop, entry_mid + 2.50 * atr15)
            risk=stop-entry_mid
            tp1=entry_mid-risk; tp2=entry_mid-1.7*risk; tp3=entry_mid-2.5*risk
            if block_short:
                veto.append("SHORT растянут: не догонять движение вниз.")
        if risk<=0:
            veto.append("Некорректная геометрия риска; направление считать только контекстом.")
        else:
            rr=abs(tp2-entry_mid)/risk
            stop_atr=risk/atr15
            target_atr=abs(tp2-entry_mid)/atr15
            if risk/entry_mid>.025:
                veto.append("Структурный стоп превышает 2.5% от цены входа.")
            if rr<1.5:
                veto.append(f"R:R {rr:.2f} ниже 1.5.")

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
            warnings.append(f"Активен перенос целей: {targets_hit} предыдущих целей уже пройдено; новые цели перестроены по структуре 15м/1ч.")

    # V1.7: primary Time Engine is now forward-path simulation from the CURRENT state.
    # Historical analogs remain only as a calibration/quality reference below.
    flow5_for_model = None if d5flow is None else float(d5flow) * min(1.0, f5["coverage_min"] / 4.0)
    flow15_for_model = None if d15flow is None else float(d15flow) * min(1.0, f15["coverage_min"] / 12.0)
    lob_imb,lob_spread,lob_source=order_book_imbalance(50)
    fwd=future_analog_outlook(price,flow5_for_model,flow15_for_model,float(oi_ch or 0.0),funding,lob_imbalance=lob_imb)
    try:
        news_ctx=news_snapshot()
    except Exception:
        news_ctx=None
    fwd, news_adj = apply_news_to_forward(fwd, news_ctx)

    # V1.21: use a REAL liquidation map when a provider key is configured.
    # It is deliberately bounded: liquidation clusters are a forward feature, not a signal by themselves.
    # Liquidation data is OPTIONAL and isolated from the core forecast.
    # Any provider/API problem must degrade to "map unavailable", never to radar 500.
    liq_ctx=liquidation_snapshot(price)
    try:
        fwd=apply_liquidation_to_forward(fwd, liq_ctx)
    except Exception:
        pass

    # V1.21: a new forecast is clocked, not candle-clocked. A newly relevant
    # macro/geopolitical event may force ONE immediate re-evaluation.
    force_news=False
    if news_adj.get('force_refresh') and news_adj.get('event_id'):
        prev_event=kv_get('last_forecast_news_event_v120')
        force_news=(prev_event != news_adj.get('event_id'))
        if force_news:
            kv_set('last_forecast_news_event_v120',news_adj.get('event_id'))
    fwd=stabilize_future_forecast(fwd, force_refresh=force_news)

    # Keep compatibility values aligned with the locked 1h snapshot.
    fwd['expected_60m_low']=fwd['expected_1h_low']; fwd['expected_60m_mid']=fwd['expected_1h_mid']; fwd['expected_60m_high']=fwd['expected_1h_high']
    # V1.20: main decision is the future-labelled analogue forecast, not the latest candle,
    # with a bounded macro/news adjustment when the market confirms the event.
    # Scores remain diagnostics, but they no longer define the live trade direction.
    trade_signal=fwd['direction_1h']
    trade_reason=f"Прогноз будущего на 1ч: {trade_signal} · {fwd['probability_1h']}%. Модель ищет похожие состояния и оценивает, какой направленный ход происходил ПОСЛЕ них первым."

    # Entry is the live price. TP/SL are rebuilt from the forward distributions.
    live_entry, stop, tp1 = current_price_levels(price, trade_signal, fwd, atr15, d1m, d5, d15)
    tp2=tp3=None
    entry_low=entry_high=live_entry
    entry_mid=live_entry
    risk=abs(entry_mid-stop)
    rr=abs(tp1-entry_mid)/risk if risk>0 else None
    stop_atr=risk/atr15 if risk>0 else stop_atr
    target_atr=abs(tp1-entry_mid)/atr15 if risk>0 else target_atr

    analog=time_to_event_analogs(d15_raw,trade_signal,stop_atr,target_atr,16) if trade_signal else {"analog_count":0}

    first_med=fwd.get("first_event_median_min")
    expiry=45 if first_med is None else int(max(30,min(180,round((first_med*.75)/15)*15)))

    # Data confidence is coverage/quality, not trade probability.
    quality=[]; confidence=0
    if all(len(df) >= 100 for df in (d4,d1,d15,d5,d1m)):
        confidence += 35; quality.append("Закрытые свечи 4ч/1ч/15м/5м/1м: ОК (+35)")
    if oi_ch is not None:
        confidence += 15; quality.append(f"Открытый интерес ~30м: ОК, источник {oi_source} (+15)")
    elif oi_source != "Unavailable":
        confidence += 5; quality.append(f"Открытый интерес: текущий снимок ОК ({oi_source}); история набирается {oi_coverage:.0f}/30 мин (+5/15)")
    else:
        quality.append("Открытый интерес: все публичные источники недоступны")
    
    if funding_info.get("source") != "Unavailable":
        confidence += 10; quality.append(f"Фандинг: ОК, источник {funding_info.get('source')} (+10)")
    else:
        quality.append("Фандинг недоступен (0 используется только как нейтральное значение)")
    if f5["coverage_min"] >= 4.0 and f15["coverage_min"] >= 12.0:
        confidence += 15; quality.append("Поток 5м/15м: окна полностью сформированы (+15)")
    elif f5["coverage_min"] >= 4.0:
        confidence += 9; quality.append(f"Поток 5м готов; 15м прогревается {f15['coverage_min']:.1f}m (+9/15)")
    elif f5["coverage_min"] > 0:
        tf_pts=max(3,int(round(9*f5["coverage_min"]/4)))
        confidence += tf_pts; quality.append(f"Поток прогревается {f5['coverage_min']:.1f}m (+{tf_pts}/15)")
    else: quality.append("Поток недоступен")
    ac=int(analog.get("analog_count") or 0)
    # Future-labelled analogue model: support is the number of similar historical states used.
    fm_pts=15 if fwd.get('path_count',0)>=250 else 10
    confidence += fm_pts
    quality.append(f"Предсказатель будущего: похожие исторические состояния с известным результатом после них, поддержка {fwd.get('path_count',0)} (+{fm_pts}/15)")
    if news_adj.get("used"):
        quality.append(news_adj.get("reason"))
    if lob_imb is not None:
        quality.append(f"Стакан L2: {lob_source}, дисбаланс {lob_imb:+.1f}% (только корректировка прогноза)")
    else:
        quality.append("Новости: активного подтверждённого влияния на направление сейчас нет")
    if liq_ctx.get("available"):
        confidence += 5
        quality.append(f"Карта ликвидаций: {liq_ctx.get('provider')} · {liq_ctx.get('source_mode')} · bias {liq_ctx.get('bias')} · доминирование {liq_ctx.get('dominance')}% (+5)")
    else:
        quality.append(liq_ctx.get("note") or "Настоящая карта ликвидаций не подключена")
    quality.append("История используется как обучающая выборка: признаки сейчас → фактическое направление спустя 1ч/6ч/12ч")
    if len(d15_raw) and pd.notna(d15_raw.iloc[-1]["close_time"]):
        confidence += 10; quality.append("Время последней закрытой свечи 15м: ОК (+10)")
    confidence=max(0,min(100,int(confidence)))

    rd=lambda x: round(float(x),2) if x is not None else None
    return Result(
        timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        symbol=SYMBOL, price=rd(price),
        last_closed_15m_utc=d15_raw.iloc[-1]["close_time"].isoformat(),
        regime=regime, bias=bias, entry_status=entry_status, stage=stage,
        market_stage=market_stage, targets_hit=targets_hit, target_rollover=target_rollover, fast_bias=fast_bias,
        forecast_direction_1h=fwd['direction_1h'], forecast_probability_1h=fwd['probability_1h'],
        forecast_direction_6h=fwd['direction_6h'], forecast_probability_6h=fwd['probability_6h'],
        forecast_direction_12h=fwd['direction_12h'], forecast_probability_12h=fwd['probability_12h'],
        forecast_eta_1h_min=fwd.get('eta_1h_min'), forecast_eta_6h_min=fwd.get('eta_6h_min'), forecast_eta_12h_min=fwd.get('eta_12h_min'),
        forecast_up_15m=fwd['up_15m'], forecast_range_15m=fwd['range_15m'], forecast_down_15m=fwd['down_15m'],
        forecast_up_60m=fwd['up_60m'], forecast_range_60m=fwd['range_60m'], forecast_down_60m=fwd['down_60m'],
        expected_60m_low=fwd['expected_60m_low'], expected_60m_mid=fwd['expected_60m_mid'], expected_60m_high=fwd['expected_60m_high'],
        breakout_up_level=fwd['breakout_up_level'], breakdown_level=fwd['breakdown_level'],
        p_breakout_up_60m=fwd['p_breakout_up_60m'], p_breakdown_60m=fwd['p_breakdown_60m'],
        momentum_delta=fwd['momentum_delta'], forecast_confidence=fwd['forecast_confidence'], forward_path_count=fwd['path_count'],
        trade_signal=trade_signal, trade_signal_reason=trade_reason, p_tp2_60m=fwd['p_target_60m'],
        data_confidence=confidence, data_quality=quality,
        long_score=ls, short_score=ss, signal=trade_signal,
        entry_low=rd(entry_low), entry_high=rd(entry_high), stop=rd(stop),
        tp1=rd(tp1), tp2=rd(tp2), tp3=rd(tp3), rr_tp2=round(rr,2) if rr else None,
        funding_rate_pct=round(funding,5), oi_change_pct=round(float(oi_ch),3) if oi_ch is not None else None,
        oi_source=oi_source, oi_coverage_min=float(oi_coverage),
        liquidation_available=bool(liq_ctx.get("available")), liquidation_provider=str(liq_ctx.get("provider") or "Не подключено"),
        liquidation_mode=str(liq_ctx.get("source_mode") or "none"), liquidation_bias=str(liq_ctx.get("bias") or "NEUTRAL"),
        liquidation_dominance=float(liq_ctx.get("dominance") or 0.0),
        liquidation_nearest_above=rd(liq_ctx.get("nearest_above")), liquidation_nearest_below=rd(liq_ctx.get("nearest_below")),
        liquidation_strongest_above=rd(liq_ctx.get("strongest_above")), liquidation_strongest_below=rd(liq_ctx.get("strongest_below")),
        liquidation_adjustment_1h_pp=float(liq_ctx.get("forecast_adjustment_1h_pp") or 0.0),
        liquidation_levels_used=int(liq_ctx.get("levels_used") or 0), liquidation_note=str(liq_ctx.get("note") or ""),
        cvd_quote=round(cvd,2),
        flow_5m_pct=f5.get("delta_pct"), flow_15m_pct=f15.get("delta_pct"),
        flow_5m_coverage_min=float(f5.get("coverage_min",0.0)),
        flow_15m_coverage_min=float(f15.get("coverage_min",0.0)),
        expiry_minutes=expiry, entry_window=entry_window,
        analog_count=int(fwd["path_count"]),
        p_tp_first=fwd["p_tp_first"], p_sl_first=fwd["p_sl_first"],
        p_neither_4h=fwd["p_neither"],
        tp_time_p25_min=fwd["tp_time_p25_min"],
        tp_time_median_min=fwd["tp_time_median_min"],
        tp_time_p75_min=fwd["tp_time_p75_min"],
        reasons_long=rl, reasons_short=rs, vetoes=veto, warnings=warnings
    )
