from __future__ import annotations
import time

MIN_PRIORITY_GAP = 5


def priority_from_scores(long_score: int | float, short_score: int | float, min_gap: int = MIN_PRIORITY_GAP):
    """Return LONG/SHORT only when one side leads by at least min_gap points."""
    long_score = float(long_score)
    short_score = float(short_score)
    gap = long_score - short_score
    if gap >= min_gap:
        return "LONG"
    if gap <= -min_gap:
        return "SHORT"
    return None


def build_priority_state(long_score, short_score, ts: float | None = None, min_gap: int = MIN_PRIORITY_GAP):
    ts = float(ts or time.time())
    p = priority_from_scores(long_score, short_score, min_gap=min_gap)
    return {
        "priority": p,
        "long": int(long_score),
        "short": int(short_score),
        "gap": abs(int(long_score) - int(short_score)),
        "ts": ts,
        "min_gap": int(min_gap),
    }


def priority_change(previous: dict | None, current: dict):
    """A change exists only between two confirmed priorities. Neutral/tie states do not alert."""
    if not previous or not current.get("priority"):
        return None
    old = previous.get("priority")
    new = current.get("priority")
    if not old or old == new:
        return None
    elapsed_min = max(0, round((float(current.get("ts", time.time())) - float(previous.get("ts", time.time()))) / 60))
    return {
        "from": old,
        "to": new,
        "elapsed_min": elapsed_min,
        "previous": previous,
        "current": current,
    }
