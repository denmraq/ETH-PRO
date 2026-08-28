from __future__ import annotations
import asyncio, os, time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel
from radar import analyze
from macro_news import snapshot as news_snapshot
from push_service import public_key, configured as push_configured, send_all
from state_store import init_db, save_subscription, kv_get, kv_set, subscriptions
from signal_priority import build_priority_state, priority_change, MIN_PRIORITY_GAP

app=FastAPI(title='ETH Entry Radar PRO V1.23')
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
def health(): return {'ok':True,'version':'pro-1.23','push':push_configured(),'monitor':os.getenv('ENABLE_MONITOR','1')=='1'}
def _safe_radar_payload():
    try:
        payload=analyze().to_dict()
        try: kv_set('last_good_radar_payload', payload)
        except Exception: pass
        return payload
    except Exception as e:
        # Never hide the cause behind a generic HTTP 500. The UI can display this
        # diagnostic, and a prior good snapshot can still keep the app usable.
        try:
            prev=kv_get('last_good_radar_payload')
        except Exception:
            prev=None
        if isinstance(prev, dict) and prev:
            out=dict(prev)
            out['_stale']=True
            out['_radar_error']=f"{type(e).__name__}: {e}"[:700]
            return out
        return {'_radar_error':f"{type(e).__name__}: {e}"[:700], '_stale':True}

@app.get('/api/radar')
def radar(): return _safe_radar_payload()
@app.get('/api/news')
def news(): return news_snapshot()
@app.get('/api/dashboard')
def dashboard(): return {'radar':_safe_radar_payload(),'news':news_snapshot(),'priority':kv_get('confirmed_priority')}
@app.get('/api/priority')
def priority(): return kv_get('confirmed_priority') or {'priority':None,'min_gap':MIN_PRIORITY_GAP}
@app.get('/api/push/public-key')
def push_key(): return {'configured':push_configured(),'public_key':public_key()}
class PushSub(BaseModel):
    endpoint:str; keys:dict
@app.post('/api/push/subscribe')
def push_subscribe(sub:PushSub):
    if not push_configured(): raise HTTPException(503,'Push не настроен на сервере: нужны VAPID_PUBLIC_KEY и VAPID_PRIVATE_KEY')
    payload=sub.model_dump()
    save_subscription(payload)
    return {'ok':True}

@app.get('/api/push/status')
def push_status():
    return {
        'configured':push_configured(),
        'subscriptions':len(subscriptions()),
        'monitor_enabled':os.getenv('ENABLE_MONITOR','1')=='1',
        'monitor_error':kv_get('monitor_error'),
        'confirmed_signal':kv_get('confirmed_trade_signal')
    }

@app.post('/api/push/test')
def push_test():
    if not push_configured(): raise HTTPException(503,'Push не настроен на сервере')
    result=send_all('ETH Radar PRO','Тест Push: уведомления работают.')
    if result.get('sent',0)<1:
        raise HTTPException(502,f"Тест не доставлен. Подписок: {result.get('subscriptions',0)}. Ошибка: {result.get('last_error')}")
    return result

async def monitor_loop():
    interval=max(60,int(os.getenv('MONITOR_INTERVAL_SECONDS','60')))
    while True:
        try:
            r=await asyncio.to_thread(analyze); n=await asyncio.to_thread(news_snapshot)

            # V1.13: Push follows the same 1h forward direction that the user sees
            # in the large LONG/SHORT card.  Old score-priority logic could disagree
            # with the visible signal and therefore appeared 'broken'.
            cur_signal=str(r.trade_signal or r.forecast_direction_1h or '').upper()
            cur_prob=float(r.forecast_probability_1h or 0.0)
            min_prob=float(os.getenv('PUSH_MIN_PROBABILITY','54'))
            needed=max(1,int(os.getenv('PUSH_CONFIRM_CYCLES','2')))
            prev=kv_get('confirmed_trade_signal')
            candidate=kv_get('push_signal_candidate') or {'signal':None,'count':0}

            if cur_signal in {'LONG','SHORT'} and cur_prob >= min_prob:
                if candidate.get('signal') == cur_signal:
                    candidate['count']=int(candidate.get('count',0))+1
                else:
                    candidate={'signal':cur_signal,'count':1,'probability':cur_prob,'first_seen_ts':time.time()}
                candidate['probability']=cur_prob
                kv_set('push_signal_candidate',candidate)

                if int(candidate.get('count',0)) >= needed:
                    if prev is None:
                        kv_set('confirmed_trade_signal',{'signal':cur_signal,'probability':cur_prob,'since_ts':time.time()})
                    elif prev.get('signal') != cur_signal:
                        result=await asyncio.to_thread(
                            send_all,
                            f'ETH Radar PRO: {cur_signal}',
                            f'Направление 1ч изменилось {prev.get("signal")} → {cur_signal} · вероятность {cur_prob:.1f}% · цена {r.price}'
                        )
                        kv_set('last_push_result',result)
                        kv_set('confirmed_trade_signal',{'signal':cur_signal,'probability':cur_prob,'since_ts':time.time()})
                    else:
                        stable=dict(prev); stable['probability']=cur_prob
                        kv_set('confirmed_trade_signal',stable)
            kv_set('last_monitor_snapshot',{'signal':cur_signal,'probability':cur_prob,'min_probability':min_prob})

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
