from __future__ import annotations
import asyncio, os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel
from radar import analyze
from macro_news import snapshot as news_snapshot
from push_service import public_key, configured as push_configured, send_all
from state_store import init_db, save_subscription, kv_get, kv_set
from signal_priority import build_priority_state, priority_change, MIN_PRIORITY_GAP

app=FastAPI(title='ETH Entry Radar PRO V1.7')
BASE=Path(__file__).parent; STATIC=BASE/'static'; app.mount('/static',StaticFiles(directory=STATIC),name='static')

@app.middleware('http')
async def no_cache(request:Request,call_next):
    r=await call_next(request)
    if request.url.path=='/' or request.url.path.startswith('/api/') or request.url.path in {'/service-worker.js','/manifest.webmanifest'}:
        r.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'; r.headers['Pragma']='no-cache'
    return r

@app.get('/')
def index(): return FileResponse(STATIC/'index.html')
@app.get('/manifest.webmanifest')
def manifest(): return FileResponse(STATIC/'manifest.webmanifest',media_type='application/manifest+json')
@app.get('/service-worker.js')
def sw(): return FileResponse(STATIC/'service-worker.js',media_type='application/javascript',headers={'Cache-Control':'no-cache'})
@app.get('/api/health')
def health(): return {'ok':True,'version':'pro-1.7','push':push_configured(),'priority_gap':MIN_PRIORITY_GAP}
@app.get('/api/radar')
def radar(): return analyze().to_dict()
@app.get('/api/news')
def news(): return news_snapshot()
@app.get('/api/dashboard')
def dashboard(): return {'radar':analyze().to_dict(),'news':news_snapshot(),'priority':kv_get('confirmed_priority')}
@app.get('/api/priority')
def priority(): return kv_get('confirmed_priority') or {'priority':None,'min_gap':MIN_PRIORITY_GAP}
@app.get('/api/push/public-key')
def push_key(): return {'configured':push_configured(),'public_key':public_key()}
class PushSub(BaseModel):
    endpoint:str; keys:dict
@app.post('/api/push/subscribe')
def push_subscribe(sub:PushSub):
    if not push_configured(): raise HTTPException(503,'Push is not configured on server')
    save_subscription(sub.model_dump()); return {'ok':True}

async def monitor_loop():
    interval=max(60,int(os.getenv('MONITOR_INTERVAL_SECONDS','60')))
    while True:
        try:
            r=await asyncio.to_thread(analyze); n=await asyncio.to_thread(news_snapshot)

            # Push logic is deliberately separate from the raw LONG/SHORT label.
            # A priority is confirmed only if one score leads by >= 5 points.
            cur=build_priority_state(r.long_score, r.short_score)
            prev=kv_get('confirmed_priority')
            change=priority_change(prev, cur)

            if cur.get('priority'):
                if prev is None:
                    # Seed state on server start without sending a noisy first notification.
                    kv_set('confirmed_priority',cur)
                elif change:
                    mins=change['elapsed_min']
                    age=f"{mins} мин" if mins else "<1 мин"
                    await asyncio.to_thread(
                        send_all,
                        'ETH Radar PRO: приоритет изменился',
                        f"{change['from']} → {change['to']} · спустя {age} · LONG {r.long_score}/100 · SHORT {r.short_score}/100"
                    )
                    kv_set('confirmed_priority',cur)
                elif prev.get('priority') == cur.get('priority'):
                    # Keep the timestamp of when this confirmed priority began; only refresh scores.
                    stable=dict(prev)
                    stable.update({'long':cur['long'],'short':cur['short'],'gap':cur['gap'],'min_gap':cur['min_gap']})
                    kv_set('confirmed_priority',stable)
            # If gap is <5, do not alert and do not erase the last confirmed priority.
            kv_set('last_monitor_snapshot',cur)

            f=n.get('focus')
            if f and f.get('impact')=='HIGH' and float(f.get('age_min') or 999)<3:
                eid=f.get('event_id')
                if kv_get('last_high_news')!=eid:
                    await asyncio.to_thread(send_all,'ETH Radar PRO: HIGH impact news',f"{f.get('source')}: {f.get('title')} · ETH bias {f.get('eth_bias')} {f.get('score')}")
                    kv_set('last_high_news',eid)
        except Exception as e:
            kv_set('monitor_error',str(e))
        await asyncio.sleep(interval)

@app.on_event('startup')
async def startup():
    init_db()
    if os.getenv('ENABLE_MONITOR','1')=='1': asyncio.create_task(monitor_loop())
