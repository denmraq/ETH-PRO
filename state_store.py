from __future__ import annotations
import json, os, sqlite3, threading, time
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "radar_pro.sqlite3"
_LOCK = threading.RLock()

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with _LOCK, _conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS news_events(
          event_id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT,
          published_ts REAL, first_seen_ts REAL, impact TEXT, semantic_bias INTEGER,
          baseline_eth REAL, baseline_btc REAL, payload TEXT
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions(
          endpoint TEXT PRIMARY KEY, subscription_json TEXT NOT NULL, created_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, updated_ts REAL);
        CREATE TABLE IF NOT EXISTS flow_trades(exec_id TEXT PRIMARY KEY, ts REAL, signed_quote REAL, quote_value REAL);
        CREATE INDEX IF NOT EXISTS idx_flow_ts ON flow_trades(ts);
        ''')

def upsert_news(event: dict):
    init_db()
    with _LOCK, _conn() as c:
        c.execute('''INSERT INTO news_events(event_id,source,title,url,published_ts,first_seen_ts,impact,semantic_bias,baseline_eth,baseline_btc,payload)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(event_id) DO UPDATE SET payload=excluded.payload''',(
          event['event_id'],event.get('source'),event.get('title'),event.get('url'),event.get('published_ts'),
          event.get('first_seen_ts'),event.get('impact'),event.get('semantic_bias',0),event.get('baseline_eth'),
          event.get('baseline_btc'),json.dumps(event,ensure_ascii=False)))

def get_news(event_id: str):
    init_db()
    with _LOCK, _conn() as c:
        r=c.execute("SELECT * FROM news_events WHERE event_id=?",(event_id,)).fetchone()
    return dict(r) if r else None

def recent_news(limit=25, max_age_hours=24):
    init_db(); cutoff=time.time()-max_age_hours*3600
    with _LOCK, _conn() as c:
        rows=c.execute("SELECT * FROM news_events WHERE first_seen_ts>=? ORDER BY first_seen_ts DESC LIMIT ?",(cutoff,limit)).fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        try: d['payload']=json.loads(d.get('payload') or '{}')
        except Exception: d['payload']={}
        out.append(d)
    return out

def save_subscription(sub: dict):
    init_db(); ep=str(sub.get('endpoint',''))
    if not ep: raise ValueError('subscription endpoint missing')
    with _LOCK, _conn() as c:
        c.execute("INSERT OR REPLACE INTO push_subscriptions(endpoint,subscription_json,created_ts) VALUES(?,?,?)",
                  (ep,json.dumps(sub),time.time()))

def delete_subscription(endpoint: str):
    init_db()
    with _LOCK, _conn() as c: c.execute("DELETE FROM push_subscriptions WHERE endpoint=?",(endpoint,))

def subscriptions():
    init_db()
    with _LOCK, _conn() as c: rows=c.execute("SELECT subscription_json FROM push_subscriptions").fetchall()
    out=[]
    for r in rows:
        try: out.append(json.loads(r[0]))
        except Exception: pass
    return out

def kv_get(key, default=None):
    init_db()
    with _LOCK, _conn() as c: r=c.execute("SELECT v FROM kv WHERE k=?",(key,)).fetchone()
    if not r: return default
    try: return json.loads(r[0])
    except Exception: return r[0]

def kv_set(key, value):
    init_db()
    with _LOCK, _conn() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v,updated_ts) VALUES(?,?,?)",(key,json.dumps(value),time.time()))


def save_flow_trades(rows):
    init_db(); now=time.time(); cutoff=now-20*60
    with _LOCK, _conn() as c:
        c.executemany("INSERT OR IGNORE INTO flow_trades(exec_id,ts,signed_quote,quote_value) VALUES(?,?,?,?)", rows)
        c.execute("DELETE FROM flow_trades WHERE ts<?",(cutoff,))

def flow_window(minutes:int):
    init_db(); now=time.time(); cutoff=now-minutes*60
    with _LOCK, _conn() as c:
        rows=c.execute("SELECT ts,signed_quote,quote_value FROM flow_trades WHERE ts>=?",(cutoff,)).fetchall()
    if not rows: return {"delta_pct":None,"cvd_quote":0.0,"quote_total":0.0,"coverage_min":0.0,"count":0}
    signed=sum(float(r['signed_quote']) for r in rows); total=sum(float(r['quote_value']) for r in rows); oldest=min(float(r['ts']) for r in rows)
    coverage=min(minutes,max(0.0,(now-oldest)/60))
    return {"delta_pct":round(signed/total*100,2) if total else 0.0,"cvd_quote":signed,"quote_total":total,"coverage_min":round(coverage,1),"count":len(rows)}
