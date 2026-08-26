import pandas as pd
import numpy as np
from radar import indicators, time_to_event_analogs, classify_direction, entry_zone


def make_df(n=400):
    t = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    base = 2000 + np.sin(np.arange(n)/8)*20 + np.arange(n)*0.08
    close = base + np.sin(np.arange(n)/3)*2
    return pd.DataFrame({
        "open_time": t,
        "open": close - 1,
        "high": close + 5,
        "low": close - 5,
        "close": close,
        "volume": 1000.0,
        "close_time": t + pd.Timedelta(minutes=15),
    })


def test_indicators_columns():
    x = indicators(make_df())
    for c in ["ema20", "ema50", "rsi14", "atr14", "atr_pct", "prior20_high", "prior20_low"]:
        assert c in x.columns


def test_direction_always_long_or_short():
    assert classify_direction(80, 20) == "LONG"
    assert classify_direction(20, 80) == "SHORT"
    assert classify_direction(50, 50, tie_long=True) == "LONG"
    assert classify_direction(50, 50, tie_long=False) == "SHORT"


def test_entry_zone_is_range():
    zone = entry_zone("LONG", 2500.0, 2490.0, 20.0)
    assert zone[0] <= zone[1]
    zone = entry_zone("SHORT", 2500.0, 2510.0, 20.0)
    assert zone[0] <= zone[1]


def test_time_engine_probabilities():
    out = time_to_event_analogs(make_df(), "LONG", .8, 1.2, 12)
    if out["analog_count"]:
        total = out["p_tp_first"] + out["p_sl_first"] + out["p_neither"]
        assert abs(total - 100) <= 0.2
