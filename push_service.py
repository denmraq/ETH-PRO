from __future__ import annotations
import json, os
from pathlib import Path
from state_store import subscriptions, delete_subscription

PUBLIC=os.getenv('VAPID_PUBLIC_KEY','').strip()
PRIVATE=os.getenv('VAPID_PRIVATE_KEY','').strip()
SUBJECT=os.getenv('VAPID_SUBJECT','mailto:radar@example.com').strip()

def configured(): return bool(PUBLIC and PRIVATE)
def public_key(): return PUBLIC

def send_all(title: str, body: str, url: str='/'):
    if not configured(): return {'sent':0,'failed':0,'configured':False}
    from pywebpush import webpush, WebPushException
    sent=failed=0
    payload=json.dumps({'title':title,'body':body,'url':url},ensure_ascii=False)
    for sub in subscriptions():
        try:
            webpush(subscription_info=sub,data=payload,vapid_private_key=PRIVATE,vapid_claims={'sub':SUBJECT},ttl=120)
            sent+=1
        except Exception as e:
            failed+=1
            # 404/410 = stale browser subscription.
            status=getattr(getattr(e,'response',None),'status_code',None)
            if status in (404,410): delete_subscription(sub.get('endpoint',''))
    return {'sent':sent,'failed':failed,'configured':True}
