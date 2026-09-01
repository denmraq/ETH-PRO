import asyncio
import json
import math
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import httpx
import uvicorn
import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

SYMBOL = os.getenv("SYMBOL", "ETHUSDT").upper()
WS_URL = os.getenv("BYBIT_WS", "wss://stream.bybit.com/v5/public/linear")
REST_URL = os.getenv("BYBIT_REST", "https://api.bybit.com")
BOOK_DEPTH = int(os.getenv("BOOK_DEPTH", "200"))
PORT = int(os.getenv("PORT", "8000"))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def safe_div(a, b, default=0.0):
    return a / b if b else default


def now_ms():
    return int(time.time() * 1000)


@dataclass
class Trade:
    ts: int
    price: float
    qty: float
    side: str  # Buy = taker buy, Sell = taker sell


@dataclass
class Liq:
    ts: int
    price: float
    qty: float
    side: str


@dataclass
class State:
    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    trades: Deque[Trade] = field(default_factory=lambda: deque(maxlen=50000))
    liquidations: Deque[Liq] = field(default_factory=lambda: deque(maxlen=5000))
    mid_history: Deque[Tuple[int, float]] = field(default_factory=lambda: deque(maxlen=6000))
    book_imbalance_history: Deque[Tuple[int, float]] = field(default_factory=lambda: deque(maxlen=6000))
    wall_history: Deque[Tuple[int, float, float, str]] = field(default_factory=lambda: deque(maxlen=10000))
    last_price: float = 0.0
    mark_price: float = 0.0
    funding: float = 0.0
    oi: float = 0.0
    oi_prev: float = 0.0
    updated_ms: int = 0
    ws_ok: bool = False
    rest_ok: bool = False
    reconnects: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def cleanup(self):
        cutoff = now_ms() - 60 * 60 * 1000
        while self.trades and self.trades[0].ts < cutoff:
            self.trades.popleft()
        while self.liquidations and self.liquidations[0].ts < cutoff:
            self.liquidations.popleft()
        while self.mid_history and self.mid_history[0][0] < cutoff:
            self.mid_history.popleft()
        while self.book_imbalance_history and self.book_imbalance_history[0][0] < cutoff:
            self.book_imbalance_history.popleft()


state = State()
app = FastAPI(title="ETH Order Flow Radar", version="1.0.0")


def apply_book(side_map: Dict[float, float], rows: List[List[str]], snapshot=False):
    if snapshot:
        side_map.clear()
    for p, q in rows:
        price = float(p)
        qty = float(q)
        if qty == 0:
            side_map.pop(price, None)
        else:
            side_map[price] = qty


def top_book(n=200):
    bids = sorted(state.bids.items(), key=lambda x: x[0], reverse=True)[:n]
    asks = sorted(state.asks.items(), key=lambda x: x[0])[:n]
    return bids, asks


def weighted_book_imbalance(levels=50):
    bids, asks = top_book(levels)
    if not bids or not asks:
        return 0.0
    mid = (bids[0][0] + asks[0][0]) / 2
    def score(rows):
        total = 0.0
        for price, qty in rows:
            dist_bps = abs(price - mid) / mid * 10000
            weight = 1.0 / (1.0 + dist_bps / 4.0)
            total += qty * weight
        return total
    bv, av = score(bids), score(asks)
    return safe_div(bv - av, bv + av)


def trades_in(ms):
    cutoff = now_ms() - ms
    return [t for t in state.trades if t.ts >= cutoff]


def trade_flow(ms):
    rows = trades_in(ms)
    buy = sum(t.qty for t in rows if t.side == "Buy")
    sell = sum(t.qty for t in rows if t.side == "Sell")
    total = buy + sell
    delta = buy - sell
    return {
        "buy": buy,
        "sell": sell,
        "delta": delta,
        "ratio": safe_div(delta, total),
        "count": len(rows),
    }


def price_change(ms):
    cutoff = now_ms() - ms
    vals = [(ts, p) for ts, p in state.mid_history if ts >= cutoff]
    if len(vals) < 2:
        return 0.0
    return safe_div(vals[-1][1] - vals[0][1], vals[0][1])


def realized_range(ms=5*60*1000):
    cutoff = now_ms() - ms
    prices = [t.price for t in state.trades if t.ts >= cutoff]
    if len(prices) < 10:
        return 0.0
    return max(prices) - min(prices)


def detect_absorption():
    # Strong aggressive flow with little/no price progress = passive absorption.
    f60 = trade_flow(60_000)
    ch60 = price_change(60_000)
    intensity = abs(f60["ratio"])
    if f60["count"] < 20 or intensity < 0.16:
        return "NONE", 0.0
    expected = f60["ratio"] * 0.0015
    # Buy aggression without upward progress -> seller absorbs -> bearish.
    if f60["ratio"] > 0 and ch60 < max(0.00015, expected * 0.20):
        return "SELLER_ABSORPTION", clamp(intensity * 1.8, 0, 1)
    if f60["ratio"] < 0 and ch60 > min(-0.00015, expected * 0.20):
        return "BUYER_ABSORPTION", clamp(intensity * 1.8, 0, 1)
    return "NONE", 0.0


def detect_exhaustion():
    f30 = trade_flow(30_000)
    f180 = trade_flow(180_000)
    if f180["count"] < 50:
        return "NONE", 0.0
    # 3m had strong one-sided flow, last 30s fades/reverses.
    if f180["ratio"] > 0.12 and f30["ratio"] < 0.02:
        return "BUY_EXHAUSTION", clamp((f180["ratio"] - f30["ratio"]) * 2.5, 0, 1)
    if f180["ratio"] < -0.12 and f30["ratio"] > -0.02:
        return "SELL_EXHAUSTION", clamp((f30["ratio"] - f180["ratio"]) * 2.5, 0, 1)
    return "NONE", 0.0


def liquidation_pressure(ms=5*60*1000):
    cutoff = now_ms() - ms
    longs = 0.0
    shorts = 0.0
    for x in state.liquidations:
        if x.ts < cutoff:
            continue
        # Bybit docs: S=Buy means long position liquidated; Sell means short liquidated.
        if x.side == "Buy":
            longs += x.qty
        else:
            shorts += x.qty
    total = longs + shorts
    return longs, shorts, safe_div(shorts - longs, total)


def local_structure():
    # Pure microstructure from recent trade prices: compare robust medians of sequential windows.
    rows = list(state.trades)
    if len(rows) < 80:
        return 0.0
    recent = rows[-40:]
    prev = rows[-80:-40]
    p1 = statistics.median(t.price for t in recent)
    p0 = statistics.median(t.price for t in prev)
    rng = realized_range(5 * 60_000)
    scale = max(rng, state.last_price * 0.0008, 1e-9)
    return clamp((p1 - p0) / scale, -1, 1)


def liquidity_clusters(direction: str):
    bids, asks = top_book(BOOK_DEPTH)
    rows = asks if direction == "LONG" else bids
    if not rows or state.last_price <= 0:
        return []
    quantities = [q for _, q in rows]
    if len(quantities) < 10:
        return []
    med = statistics.median(quantities)
    mad = statistics.median(abs(q - med) for q in quantities) or med or 1e-9
    candidates = []
    for price, qty in rows:
        if direction == "LONG" and price <= state.last_price:
            continue
        if direction == "SHORT" and price >= state.last_price:
            continue
        dist_bps = abs(price - state.last_price) / state.last_price * 10000
        if dist_bps < 3:  # avoid simply choosing the spread-neighbour level
            continue
        z = (qty - med) / mad
        if z < 2.0 and qty < med * 2.5:
            continue
        # Structural target score: size anomaly + accessible distance. No entry-percentage target formula.
        proximity = 1 / (1 + dist_bps / 35)
        size_strength = clamp(math.log1p(max(qty / max(med, 1e-9), 1)) / math.log(12), 0, 2)
        score = size_strength * 0.72 + proximity * 0.28
        candidates.append((score, price, qty, dist_bps))
    candidates.sort(reverse=True)
    return candidates[:8]


def choose_market_target(direction: str):
    cands = liquidity_clusters(direction)
    if not cands:
        # Fallback is a recent traded structural extreme, never a fixed % from entry.
        rows = trades_in(15 * 60_000)
        if not rows:
            return None, "NO_TARGET_DATA", 0.0
        if direction == "LONG":
            price = max(t.price for t in rows)
        else:
            price = min(t.price for t in rows)
        if (direction == "LONG" and price <= state.last_price) or (direction == "SHORT" and price >= state.last_price):
            return None, "NO_TARGET_DATA", 0.0
        return price, "RECENT_TRADED_EXTREME", 0.35
    # Prefer a strong, not absurdly distant cluster; top score already includes proximity.
    score, price, qty, dist_bps = cands[0]
    return price, "ORDERBOOK_LIQUIDITY_CLUSTER", clamp(score / 1.4, 0.35, 0.95)


def compute_signal():
    if not state.bids or not state.asks or state.last_price <= 0:
        return {
            "symbol": SYMBOL,
            "direction": "LONG",  # invariant: never WAIT
            "strength": 0,
            "status": "STARTING",
            "price": state.last_price or None,
            "target": None,
            "target_basis": "WAITING_FOR_LIVE_DATA",
        }

    imb = weighted_book_imbalance(50)
    f30 = trade_flow(30_000)
    f300 = trade_flow(5 * 60_000)
    struct = local_structure()
    abs_name, abs_strength = detect_absorption()
    ex_name, ex_strength = detect_exhaustion()
    liq_long, liq_short, liq_bias = liquidation_pressure()

    oi_change = safe_div(state.oi - state.oi_prev, state.oi_prev) if state.oi_prev else 0.0
    price5 = price_change(5 * 60_000)

    # Positive = LONG, negative = SHORT.
    components = {
        "orderbook": clamp(imb, -1, 1),
        "flow_30s": clamp(f30["ratio"], -1, 1),
        "flow_5m": clamp(f300["ratio"], -1, 1),
        "structure": struct,
        "liquidations": clamp(liq_bias, -1, 1),
        "oi_confirmation": 0.0,
        "funding_contrarian": 0.0,
        "absorption": 0.0,
        "exhaustion": 0.0,
    }

    # OI is useful when it confirms direction rather than standing alone.
    if abs(oi_change) > 0.0002:
        if price5 > 0 and oi_change > 0:
            components["oi_confirmation"] = clamp(oi_change * 120, 0, 1)
        elif price5 < 0 and oi_change > 0:
            components["oi_confirmation"] = -clamp(oi_change * 120, 0, 1)
        elif price5 > 0 and oi_change < 0:
            components["oi_confirmation"] = 0.15  # short covering: weak bullish continuation info
        elif price5 < 0 and oi_change < 0:
            components["oi_confirmation"] = -0.15

    # Funding only at extremes, small weight.
    if state.funding > 0.0005:
        components["funding_contrarian"] = -clamp(state.funding / 0.002, 0, 1)
    elif state.funding < -0.0005:
        components["funding_contrarian"] = clamp(abs(state.funding) / 0.002, 0, 1)

    if abs_name == "SELLER_ABSORPTION":
        components["absorption"] = -abs_strength
    elif abs_name == "BUYER_ABSORPTION":
        components["absorption"] = abs_strength

    if ex_name == "BUY_EXHAUSTION":
        components["exhaustion"] = -ex_strength
    elif ex_name == "SELL_EXHAUSTION":
        components["exhaustion"] = ex_strength

    weights = {
        "orderbook": 0.21,
        "flow_30s": 0.18,
        "flow_5m": 0.17,
        "structure": 0.12,
        "liquidations": 0.08,
        "oi_confirmation": 0.08,
        "funding_contrarian": 0.03,
        "absorption": 0.09,
        "exhaustion": 0.04,
    }
    raw = sum(components[k] * weights[k] for k in weights)
    direction = "LONG" if raw >= 0 else "SHORT"

    # Strength measures edge magnitude + live data maturity, not win probability.
    maturity = clamp(f300["count"] / 250, 0.25, 1.0)
    strength = round(clamp(abs(raw) / 0.50 * 100 * (0.65 + 0.35 * maturity), 1, 100))

    target, basis, target_quality = choose_market_target(direction)
    opp_clusters = liquidity_clusters("SHORT" if direction == "LONG" else "LONG")
    invalidation = opp_clusters[0][1] if opp_clusters else None

    reasons = []
    ranked = sorted(components.items(), key=lambda kv: abs(kv[1] * weights[kv[0]]), reverse=True)
    for name, val in ranked[:5]:
        if abs(val) < 0.04:
            continue
        reasons.append({"factor": name, "bias": "LONG" if val > 0 else "SHORT", "value": round(val, 3)})

    return {
        "symbol": SYMBOL,
        "direction": direction,
        "strength": strength,
        "raw_edge": round(raw, 4),
        "price": round(state.last_price, 2),
        "mark": round(state.mark_price, 2) if state.mark_price else None,
        "target": round(target, 2) if target else None,
        "target_basis": basis,
        "target_quality": round(target_quality * 100),
        "invalidation_liquidity": round(invalidation, 2) if invalidation else None,
        "orderbook_imbalance": round(imb * 100, 1),
        "flow_30s": round(f30["ratio"] * 100, 1),
        "flow_5m": round(f300["ratio"] * 100, 1),
        "cvd_5m": round(f300["delta"], 3),
        "absorption": abs_name,
        "exhaustion": ex_name,
        "oi": round(state.oi, 3),
        "oi_change_pct": round(oi_change * 100, 4),
        "funding_pct": round(state.funding * 100, 5),
        "liquidated_longs_5m": round(liq_long, 3),
        "liquidated_shorts_5m": round(liq_short, 3),
        "reasons": reasons,
        "updated_ms": state.updated_ms,
        "feed": {"websocket": state.ws_ok, "rest": state.rest_ok, "reconnects": state.reconnects},
        "note": "Strength is directional edge score, not a promised win probability. Target is market-structure/orderbook based, not a fixed percentage from entry.",
    }


async def ws_loop():
    topics = [f"orderbook.{BOOK_DEPTH}.{SYMBOL}", f"publicTrade.{SYMBOL}", f"allLiquidation.{SYMBOL}", f"tickers.{SYMBOL}"]
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, max_size=8_000_000) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                state.ws_ok = True
                async for raw in ws:
                    msg = json.loads(raw)
                    topic = msg.get("topic", "")
                    data = msg.get("data")
                    async with state.lock:
                        if topic.startswith("orderbook.") and isinstance(data, dict):
                            snap = msg.get("type") == "snapshot" or data.get("u") == 1
                            apply_book(state.bids, data.get("b", []), snapshot=snap)
                            apply_book(state.asks, data.get("a", []), snapshot=snap)
                            bids, asks = top_book(1)
                            if bids and asks:
                                mid = (bids[0][0] + asks[0][0]) / 2
                                state.mid_history.append((msg.get("ts", now_ms()), mid))
                                im = weighted_book_imbalance(50)
                                state.book_imbalance_history.append((msg.get("ts", now_ms()), im))
                                state.updated_ms = now_ms()
                        elif topic.startswith("publicTrade."):
                            for t in data or []:
                                tr = Trade(int(t.get("T", msg.get("ts", now_ms()))), float(t["p"]), float(t["v"]), t["S"])
                                state.trades.append(tr)
                                state.last_price = tr.price
                                state.updated_ms = now_ms()
                        elif topic.startswith("allLiquidation."):
                            for x in data or []:
                                state.liquidations.append(Liq(int(x["T"]), float(x["p"]), float(x["v"]), x["S"]))
                        elif topic.startswith("tickers.") and isinstance(data, dict):
                            if data.get("lastPrice"):
                                state.last_price = float(data["lastPrice"])
                            if data.get("markPrice"):
                                state.mark_price = float(data["markPrice"])
                            if data.get("fundingRate"):
                                state.funding = float(data["fundingRate"])
                            if data.get("openInterest"):
                                new_oi = float(data["openInterest"])
                                if state.oi and new_oi != state.oi:
                                    state.oi_prev = state.oi
                                state.oi = new_oi
                            state.updated_ms = now_ms()
                        state.cleanup()
        except Exception as e:
            state.ws_ok = False
            state.reconnects += 1
            print("WS reconnect:", repr(e), flush=True)
            await asyncio.sleep(2)


async def rest_loop():
    async with httpx.AsyncClient(timeout=8.0) as client:
        while True:
            try:
                ticker = await client.get(f"{REST_URL}/v5/market/tickers", params={"category": "linear", "symbol": SYMBOL})
                tj = ticker.json()
                row = tj.get("result", {}).get("list", [])[0]
                oi_resp = await client.get(f"{REST_URL}/v5/market/open-interest", params={"category": "linear", "symbol": SYMBOL, "intervalTime": "5min", "limit": 2})
                oj = oi_resp.json().get("result", {}).get("list", [])
                async with state.lock:
                    state.last_price = float(row.get("lastPrice") or state.last_price or 0)
                    state.mark_price = float(row.get("markPrice") or state.mark_price or 0)
                    state.funding = float(row.get("fundingRate") or state.funding or 0)
                    if oj:
                        current = float(oj[0]["openInterest"])
                        previous = float(oj[1]["openInterest"]) if len(oj) > 1 else state.oi_prev
                        state.oi = current
                        state.oi_prev = previous
                    state.rest_ok = True
            except Exception as e:
                state.rest_ok = False
                print("REST refresh:", repr(e), flush=True)
            await asyncio.sleep(30)


@app.on_event("startup")
async def startup():
    asyncio.create_task(ws_loop())
    asyncio.create_task(rest_loop())


@app.get("/api/radar")
async def radar():
    async with state.lock:
        return JSONResponse(compute_signal())


@app.get("/api/health")
async def health():
    return {"ok": True, "symbol": SYMBOL, "ws": state.ws_ok, "rest": state.rest_ok, "updated_ms": state.updated_ms}


HTML = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>ETH Order Flow Radar</title><style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color-scheme:dark;background:#090d13;color:#f4f7fb}.wrap{max-width:720px;margin:auto;padding:18px}.top{display:flex;justify-content:space-between;align-items:center}.muted{color:#91a0b7}.price{font-size:42px;font-weight:900}.signal{font-size:40px;font-weight:950;margin:12px 0}.long{color:#48d597}.short{color:#ff6676}.card{background:#111823;border:1px solid #243044;border-radius:18px;padding:16px;margin:12px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.kv{background:#0d131c;border-radius:13px;padding:12px}.k{font-size:11px;color:#8190a8}.v{font-size:20px;font-weight:800;margin-top:4px}.target{font-size:31px;font-weight:900}.reason{padding:8px 0;border-bottom:1px solid #202b3a}.pill{padding:7px 10px;border-radius:999px;background:#182231;font-size:12px}.small{font-size:12px}.bar{height:9px;background:#222c3a;border-radius:10px;overflow:hidden;margin-top:8px}.fill{height:100%;background:currentColor;width:0%}@media(max-width:520px){.grid{grid-template-columns:1fr 1fr}.signal{font-size:36px}.price{font-size:38px}}</style></head><body><div class="wrap">
<div class="top"><div><b>📡 ETH ORDER FLOW RADAR</b><div class="muted small">Live microstructure · Bybit ETHUSDT Perpetual</div></div><span id="feed" class="pill">CONNECTING</span></div>
<div class="card"><div class="muted">ETH/USDT PERP</div><div id="price" class="price">—</div><div id="direction" class="signal">—</div><div><b>СИЛА ПРЕИМУЩЕСТВА: <span id="strength">—</span>/100</b><div class="bar"><div id="strengthBar" class="fill"></div></div></div></div>
<div class="card"><div class="muted">🎯 НАИБОЛЕЕ ВЕРОЯТНАЯ РЫНОЧНАЯ ЦЕЛЬ</div><div id="target" class="target">—</div><div id="targetBasis" class="muted">—</div></div>
<div class="grid">
<div class="kv"><div class="k">ORDER BOOK IMBALANCE</div><div id="book" class="v">—</div></div>
<div class="kv"><div class="k">FLOW 30s</div><div id="f30" class="v">—</div></div>
<div class="kv"><div class="k">FLOW 5m</div><div id="f5" class="v">—</div></div>
<div class="kv"><div class="k">CVD 5m (ETH)</div><div id="cvd" class="v">—</div></div>
<div class="kv"><div class="k">ABSORPTION</div><div id="abs" class="v">—</div></div>
<div class="kv"><div class="k">EXHAUSTION</div><div id="exh" class="v">—</div></div>
<div class="kv"><div class="k">OI Δ 5m</div><div id="oi" class="v">—</div></div>
<div class="kv"><div class="k">FUNDING</div><div id="fund" class="v">—</div></div>
</div>
<div class="card"><b>Почему сейчас</b><div id="reasons"></div></div>
<div class="card small muted">Сигнал всегда LONG или SHORT. Сила — это оценка перевеса текущего order flow, а не обещанная вероятность выигрыша. Цель строится по живому стакану/ликвидности либо по реально проторгованному структурному экстремуму, а не по проценту от входа.</div>
</div><script>
const $=id=>document.getElementById(id); const fmt=(x,d=2)=>x==null?'—':Number(x).toFixed(d); const signed=(x,d=1)=>x==null?'—':(x>0?'+':'')+Number(x).toFixed(d)+'%';
async function go(){try{let r=await fetch('/api/radar',{cache:'no-store'});let d=await r.json();$('price').textContent=d.price?d.price.toFixed(2):'—';$('direction').textContent=d.direction;$('direction').className='signal '+(d.direction==='LONG'?'long':'short');$('strength').textContent=d.strength;$('strengthBar').style.width=d.strength+'%';$('strengthBar').parentElement.style.color=d.direction==='LONG'?'#48d597':'#ff6676';$('target').textContent=d.target?d.target.toFixed(2)+' USDT':'Формируется';$('targetBasis').textContent=d.target_basis+(d.target_quality?' · качество цели '+d.target_quality+'/100':'');$('book').textContent=signed(d.orderbook_imbalance);$('f30').textContent=signed(d.flow_30s);$('f5').textContent=signed(d.flow_5m);$('cvd').textContent=fmt(d.cvd_5m,2);$('abs').textContent=d.absorption||'NONE';$('exh').textContent=d.exhaustion||'NONE';$('oi').textContent=signed(d.oi_change_pct,3);$('fund').textContent=signed(d.funding_pct,4);$('feed').textContent=(d.feed?.websocket?'LIVE':'RECONNECT')+' · '+(d.feed?.rest?'REST OK':'REST —');$('reasons').innerHTML=(d.reasons||[]).map(x=>`<div class="reason"><b>${x.bias}</b> · ${x.factor} <span class="muted">${x.value}</span></div>`).join('')||'<div class="muted">Набирается поток данных…</div>';}catch(e){$('feed').textContent='RECONNECT';}}
go();setInterval(go,1000);
</script></body></html>'''

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
