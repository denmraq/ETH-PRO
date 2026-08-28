
import os
import json
import time
from datetime import datetime, timezone

import requests

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = "1"
MIN_ETH = float(os.getenv("WHALE_MIN_ETH", "100"))

# Exchange addresses from the supplied whale-monitor approach.
# They are only labels for flow classification; keep this list extendable.
DEFAULT_EXCHANGE_WALLETS = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "binance_hot_wallet",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285ec": "binance_cold_wallet",
    "0x503828976d22510aad0201ac7ec88293211d23f": "coinbase",
}

def _load_json_env(name, fallback):
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return {str(k).lower(): str(v) for k, v in obj.items()}
    except Exception:
        pass
    return fallback

EXCHANGE_WALLETS = _load_json_env("EXCHANGE_WALLETS_JSON", DEFAULT_EXCHANGE_WALLETS)
WHALE_WALLETS = _load_json_env("WHALE_WALLETS_JSON", {})

_cache = {"ts": 0.0, "data": None}

def _api_key():
    return os.getenv("ETHERSCAN_API_KEY", "").strip()

def fetch_transactions(address: str, limit: int = 100):
    key = _api_key()
    if not key:
        return []

    params = {
        "chainid": CHAIN_ID,
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": key,
    }
    r = requests.get(ETHERSCAN_URL, params=params, timeout=18,
                     headers={"User-Agent": "ETH-RADAR-WHALE/1.0"})
    r.raise_for_status()
    payload = r.json()

    if payload.get("status") == "1":
        return payload.get("result", [])
    # "No transactions found" should not crash the radar.
    if payload.get("message") in ("No transactions found", "No records found"):
        return []
    return []

def _classify(tx, whale_label):
    try:
        amount = int(tx.get("value", "0")) / 1e18
    except Exception:
        return None

    if amount < MIN_ETH:
        return None

    to_addr = (tx.get("to") or "").lower()
    from_addr = (tx.get("from") or "").lower()

    direction = "internal"
    exchange = None

    if to_addr in EXCHANGE_WALLETS:
        direction = "exchange_inflow"      # possible sell pressure
        exchange = EXCHANGE_WALLETS[to_addr]
    elif from_addr in EXCHANGE_WALLETS:
        direction = "exchange_outflow"     # possible accumulation
        exchange = EXCHANGE_WALLETS[from_addr]

    if direction == "internal":
        return None

    try:
        ts = int(tx.get("timeStamp", "0"))
    except Exception:
        ts = 0

    return {
        "wallet": whale_label,
        "direction": direction,
        "exchange": exchange,
        "amount_eth": float(amount),
        "tx_hash": tx.get("hash", ""),
        "timestamp": ts,
    }

def _window(events, seconds):
    now = int(time.time())
    rows = [e for e in events if now - e["timestamp"] <= seconds]

    inflow = sum(e["amount_eth"] for e in rows if e["direction"] == "exchange_inflow")
    outflow = sum(e["amount_eth"] for e in rows if e["direction"] == "exchange_outflow")
    netflow = inflow - outflow
    total = inflow + outflow
    count = len(rows)
    max_tx = max([e["amount_eth"] for e in rows], default=0.0)

    # Conservative standalone classification:
    # do not allow a small single transfer to flip the market view.
    dominance = abs(netflow) / total if total > 0 else 0.0
    meaningful = (
        (count >= 2 and abs(netflow) >= 250 and dominance >= 0.25)
        or max_tx >= 1000
    )

    if meaningful and netflow > 0:
        signal = "EXCHANGE_INFLOW"
    elif meaningful and netflow < 0:
        signal = "ACCUMULATION"
    else:
        signal = "NEUTRAL"

    return {
        "signal": signal,
        "inflow_eth": round(inflow, 2),
        "outflow_eth": round(outflow, 2),
        "netflow_eth": round(netflow, 2),
        "events": count,
        "largest_tx_eth": round(max_tx, 2),
    }

def get_whale_snapshot(force=False):
    # Cache for one minute so page refreshes do not hammer Etherscan.
    if not force and _cache["data"] is not None and time.time() - _cache["ts"] < 60:
        return _cache["data"]

    if not _api_key():
        data = {
            "status": "needs_api_key",
            "message": "Добавь ETHERSCAN_API_KEY в Render Environment.",
            "wallets_configured": len(WHALE_WALLETS),
            "windows": {},
            "recent_events": [],
        }
        _cache.update(ts=time.time(), data=data)
        return data

    if not WHALE_WALLETS:
        data = {
            "status": "needs_wallets",
            "message": "Добавь WHALE_WALLETS_JSON в Render Environment.",
            "wallets_configured": 0,
            "windows": {},
            "recent_events": [],
        }
        _cache.update(ts=time.time(), data=data)
        return data

    seen = set()
    events = []

    for address, label in WHALE_WALLETS.items():
        try:
            txs = fetch_transactions(address, 100)
        except Exception:
            continue

        for tx in txs:
            h = tx.get("hash", "")
            if h and h in seen:
                continue
            if h:
                seen.add(h)

            event = _classify(tx, label)
            if event:
                # We only need recent history for 12h windows.
                if int(time.time()) - event["timestamp"] <= 12 * 3600:
                    events.append(event)

    events.sort(key=lambda x: x["timestamp"], reverse=True)

    data = {
        "status": "ok",
        "source": "Etherscan API V2",
        "wallets_configured": len(WHALE_WALLETS),
        "minimum_eth": MIN_ETH,
        "windows": {
            "15m": _window(events, 15 * 60),
            "1h": _window(events, 60 * 60),
            "4h": _window(events, 4 * 3600),
            "12h": _window(events, 12 * 3600),
        },
        "recent_events": [
            {
                **e,
                "time_utc": datetime.fromtimestamp(
                    e["timestamp"], tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC")
            }
            for e in events[:12]
        ],
    }

    _cache.update(ts=time.time(), data=data)
    return data
