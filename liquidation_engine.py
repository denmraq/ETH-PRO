from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

import requests

TIMEOUT = 10
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ETH-Entry-Radar-PRO/1.21"})

_COINGLASS = "https://open-api-v4.coinglass.com"
_HYBLOCK = "https://api.hyblockcapital.com/v2"
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 300.0  # liquidation maps do not need to be pulled on every UI refresh


@dataclass
class LiquidationSnapshot:
    available: bool = False
    provider: str = "Не подключено"
    source_mode: str = "none"
    current_price: Optional[float] = None
    nearest_above: Optional[float] = None
    nearest_below: Optional[float] = None
    strongest_above: Optional[float] = None
    strongest_below: Optional[float] = None
    pressure_up: float = 0.0
    pressure_down: float = 0.0
    bias: str = "NEUTRAL"
    dominance: float = 0.0
    forecast_adjustment_1h_pp: float = 0.0
    forecast_adjustment_6h_pp: float = 0.0
    forecast_adjustment_12h_pp: float = 0.0
    levels_used: int = 0
    age_sec: int = 0
    note: str = ""

    def to_dict(self):
        return asdict(self)


def _cache_get(key: str) -> Optional[dict]:
    item = _CACHE.get(key)
    if not item:
        return None
    ts, data = item
    if time.time() - ts > CACHE_TTL:
        return None
    out = dict(data)
    out["age_sec"] = int(time.time() - ts)
    return out


def _cache_set(key: str, data: dict):
    _CACHE[key] = (time.time(), dict(data))


def _coinglass_levels(range_name: str = "1d") -> list[tuple[float, float]]:
    key = os.getenv("COINGLASS_API_KEY", "").strip()
    if not key:
        return []
    r = SESSION.get(
        _COINGLASS + "/api/futures/liquidation/map",
        params={"exchange": "Binance", "symbol": "ETHUSDT", "range": range_name},
        headers={"CG-API-KEY": key, "accept": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if str(payload.get("code", "0")) not in ("0", "200"):
        raise RuntimeError(f"CoinGlass: {payload.get('msg') or payload.get('code')}")
    raw = payload.get("data", {}).get("data", {})
    out: list[tuple[float, float]] = []
    if isinstance(raw, dict):
        for _, rows in raw.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                try:
                    px = float(row[0]); size = float(row[1])
                except Exception:
                    continue
                if px > 0 and size > 0 and math.isfinite(px) and math.isfinite(size):
                    out.append((px, size))
    return out


def _hyblock_levels() -> list[tuple[float, float]]:
    key = os.getenv("HYBLOCK_API_KEY", "").strip()
    if not key:
        return []
    r = SESSION.get(
        _HYBLOCK + "/liquidationLevels",
        params={"coin": "ETH", "exchange": "binance_perp_stable", "leverage": "all", "position": "all"},
        headers={"x-api-key": key, "accept": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    rows = payload.get("data", [])
    out: list[tuple[float, float]] = []
    if isinstance(rows, list):
        for row in rows:
            try:
                px = float(row.get("price")); size = float(row.get("size"))
            except Exception:
                continue
            if px > 0 and size > 0 and math.isfinite(px) and math.isfinite(size):
                out.append((px, size))
    return out


def _summarize(levels: list[tuple[float, float]], price: float, provider: str, source_mode: str) -> dict:
    """Turn raw estimated liquidation levels into a bounded directional feature.

    A cluster above price is potential SHORT-liquidation fuel (upward magnet).
    A cluster below price is potential LONG-liquidation fuel (downward magnet).
    Distance decay prevents a huge but remote cluster from dominating a scalp forecast.
    """
    if not levels:
        return LiquidationSnapshot(note="Поставщик не вернул уровни ликвидаций").to_dict()

    # Keep levels within 8% of spot for the 1h/6h/12h trading horizon.
    usable = [(px, sz) for px, sz in levels if abs(px / price - 1.0) <= 0.08]
    if not usable:
        usable = sorted(levels, key=lambda z: abs(z[0] - price))[:80]

    above = [(px, sz) for px, sz in usable if px > price]
    below = [(px, sz) for px, sz in usable if px < price]

    def weighted(rows):
        total = 0.0
        strengths = []
        for px, sz in rows:
            dist_pct = abs(px / price - 1.0) * 100.0
            # strong preference for nearby clusters; still leaves some weight out to ~8%
            decay = math.exp(-dist_pct / 1.8)
            strength = math.log1p(max(sz, 0.0)) * decay
            total += strength
            strengths.append((strength, px))
        return total, strengths

    up, up_rows = weighted(above)
    dn, dn_rows = weighted(below)
    total = up + dn
    dominance = 0.0 if total <= 0 else abs(up - dn) / total * 100.0
    bias = "NEUTRAL"
    if total > 0 and dominance >= 12.0:
        bias = "LONG" if up > dn else "SHORT"

    nearest_above = min((px for px, _ in above), default=None)
    nearest_below = max((px for px, _ in below), default=None)
    strongest_above = max(up_rows, default=(0.0, None))[1]
    strongest_below = max(dn_rows, default=(0.0, None))[1]

    # Bounded probability correction. Liquidation map is a feature, never the decision-maker.
    edge = 0.0 if total <= 0 else (up - dn) / total
    close_bonus = 1.0
    nearest = None
    if bias == "LONG" and nearest_above:
        nearest = (nearest_above / price - 1.0) * 100.0
    elif bias == "SHORT" and nearest_below:
        nearest = (1.0 - nearest_below / price) * 100.0
    if nearest is not None:
        close_bonus = max(0.35, min(1.0, math.exp(-max(0.0, nearest - 0.35) / 2.0)))
    signed = max(-1.0, min(1.0, edge)) * close_bonus

    return LiquidationSnapshot(
        available=True,
        provider=provider,
        source_mode=source_mode,
        current_price=round(price, 2),
        nearest_above=round(nearest_above, 2) if nearest_above else None,
        nearest_below=round(nearest_below, 2) if nearest_below else None,
        strongest_above=round(strongest_above, 2) if strongest_above else None,
        strongest_below=round(strongest_below, 2) if strongest_below else None,
        pressure_up=round(up, 3),
        pressure_down=round(dn, 3),
        bias=bias,
        dominance=round(dominance, 1),
        forecast_adjustment_1h_pp=round(signed * 6.0, 1),
        forecast_adjustment_6h_pp=round(signed * 4.0, 1),
        forecast_adjustment_12h_pp=round(signed * 3.0, 1),
        levels_used=len(usable),
        note="Карта влияет только как ограниченный прогнозный фактор; сама по себе LONG/SHORT не переключает.",
    ).to_dict()


def snapshot(price: float) -> dict:
    """Get a real liquidation map when a provider key is configured.

    Priority: CoinGlass pair liquidation map -> Hyblock liquidation levels.
    If neither key exists, return an explicit 'not connected' state instead of a proxy.
    """
    cache_key = f"eth:{round(float(price), -1)}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    cg = os.getenv("COINGLASS_API_KEY", "").strip()
    hb = os.getenv("HYBLOCK_API_KEY", "").strip()
    errors = []

    if cg:
        try:
            data = _summarize(_coinglass_levels("1d"), float(price), "CoinGlass", "ETHUSDT Binance · liquidation map 1d")
            if data.get("available"):
                _cache_set(cache_key, data)
                return data
        except Exception as e:
            errors.append(f"CoinGlass: {type(e).__name__}")

    if hb:
        try:
            data = _summarize(_hyblock_levels(), float(price), "Hyblock", "ETH · Binance perpetual · liquidation levels")
            if data.get("available"):
                _cache_set(cache_key, data)
                return data
        except Exception as e:
            errors.append(f"Hyblock: {type(e).__name__}")

    note = "Настоящая карта ликвидаций не подключена: добавьте COINGLASS_API_KEY или HYBLOCK_API_KEY в Render Environment."
    if errors:
        note = "Ключ есть, но карта сейчас недоступна: " + ", ".join(errors)
    data = LiquidationSnapshot(note=note).to_dict()
    _cache_set(cache_key, data)
    return data


def _set_side_prob(fwd: dict, horizon: str, signed_edge: float):
    dkey = f"direction_{horizon}"
    pkey = f"probability_{horizon}"
    side = str(fwd.get(dkey, "LONG"))
    prob = float(fwd.get(pkey, 50.0))
    signed = (prob - 50.0) * (1.0 if side == "LONG" else -1.0)
    candidate = signed + signed_edge

    # A liquidation feature cannot flip a forecast unless the underlying model was already weak.
    if signed != 0 and candidate * signed < 0 and abs(signed) >= 3.0:
        candidate = math.copysign(max(0.3, abs(signed) - 1.0), signed)
    fwd[dkey] = "LONG" if candidate >= 0 else "SHORT"
    fwd[pkey] = round(min(82.0, 50.0 + abs(candidate)), 1)


def apply_to_forward(fwd: dict, liq: dict) -> dict:
    if not liq or not liq.get("available"):
        return fwd
    out = dict(fwd)
    _set_side_prob(out, "1h", float(liq.get("forecast_adjustment_1h_pp") or 0.0))
    _set_side_prob(out, "6h", float(liq.get("forecast_adjustment_6h_pp") or 0.0))
    _set_side_prob(out, "12h", float(liq.get("forecast_adjustment_12h_pp") or 0.0))
    return out
