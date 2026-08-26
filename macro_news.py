from __future__ import annotations
import calendar, hashlib, os, re, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests
import xml.etree.ElementTree as ET
from state_store import get_news, upsert_news, recent_news

TIMEOUT=10
UA={"User-Agent":"ETH-Entry-Radar-PRO/1.0 contact=local-user"}
FEEDS=[
    ("Federal Reserve","https://www.federalreserve.gov/feeds/press_all.xml"),
    ("BLS","https://www.bls.gov/feed/bls_latest.rss"),
    ("SEC","https://www.sec.gov/news/pressreleases.rss"),
]
CUSTOM=os.getenv("NEWS_FEEDS","").strip()
if CUSTOM:
    for i,u in enumerate([x.strip() for x in CUSTOM.split(',') if x.strip()]): FEEDS.append((f"Custom {i+1}",u))

HIGH=[r'\bfomc\b',r'federal funds',r'interest rate',r'rate decision',r'consumer price index',r'\bcpi\b',
      r'employment situation',r'nonfarm',r'payroll',r'personal consumption expenditures',r'\bpce\b',
      r'powell',r'emergency',r'financial stability']
MED=[r'producer price',r'\bppi\b',r'job openings',r'\bjolts\b',r'gross domestic product',r'\bgdp\b',
     r'retail sales',r'jobless claims',r'unemployment',r'inflation',r'etf',r'crypto',r'digital asset',r'treasur']
BULL=[r'rate cut',r'cuts? (?:the )?target',r'lower inflation',r'inflation (?:eased|cooled|fell|declined)',
      r'liquidity',r'approval of .*etf',r'approves? .*etf']
BEAR=[r'rate hike',r'raises? (?:the )?target',r'higher inflation',r'inflation (?:rose|accelerated|increased)',
      r'tighten',r'hawkish',r'rejects? .*etf',r'denies? .*etf']

def _parse_feed(content: bytes):
    out=[]
    try:
        root=ET.fromstring(content)
    except Exception:
        return out
    # RSS items
    for item in root.findall('.//item'):
        def tx(tag):
            e=item.find(tag); return (e.text or '').strip() if e is not None and e.text else ''
        out.append({'title':tx('title'),'link':tx('link'),'published':tx('pubDate') or tx('date')})
    # Atom entries
    ns={'a':'http://www.w3.org/2005/Atom'}
    for item in root.findall('.//a:entry',ns):
        title=item.find('a:title',ns); published=item.find('a:published',ns) or item.find('a:updated',ns)
        link=item.find('a:link',ns)
        out.append({'title':(title.text or '').strip() if title is not None else '',
                    'link':link.attrib.get('href','') if link is not None else '',
                    'published':(published.text or '').strip() if published is not None else ''})
    return out

def _epoch(entry):
    v=entry.get('published','')
    if v:
        try: return parsedate_to_datetime(v).timestamp()
        except Exception:
            try: return datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
            except Exception: pass
    return time.time()

def classify(text: str):
    t=(text or '').lower()
    impact='HIGH' if any(re.search(p,t) for p in HIGH) else ('MEDIUM' if any(re.search(p,t) for p in MED) else 'LOW')
    b=sum(1 for p in BULL if re.search(p,t))-sum(1 for p in BEAR if re.search(p,t))
    semantic=max(-35,min(35,b*18))
    return impact,semantic

def _ticker(symbol):
    r=requests.get('https://api.bybit.com/v5/market/tickers',params={'category':'linear','symbol':symbol},headers=UA,timeout=TIMEOUT)
    r.raise_for_status(); rows=r.json().get('result',{}).get('list',[])
    return float(rows[0]['lastPrice']) if rows else None

def ingest():
    now=time.time(); created=[]
    try: eth=_ticker('ETHUSDT'); btc=_ticker('BTCUSDT')
    except Exception: eth=btc=None
    for source,url in FEEDS:
        try:
            raw=requests.get(url,headers=UA,timeout=TIMEOUT); raw.raise_for_status(); entries=_parse_feed(raw.content)
        except Exception:
            continue
        for e in entries[:20]:
            title=str(e.get('title','')).strip(); link=str(e.get('link','')).strip()
            if not title: continue
            pub=_epoch(e)
            if now-pub > 72*3600: continue
            eid=hashlib.sha256(f'{source}|{link}|{title}'.encode()).hexdigest()[:24]
            if get_news(eid): continue
            impact,semantic=classify(title)
            item={'event_id':eid,'source':source,'title':title,'url':link,'published_ts':pub,'first_seen_ts':now,
                  'impact':impact,'semantic_bias':semantic,'baseline_eth':eth,'baseline_btc':btc}
            upsert_news(item); created.append(item)
    return created

def _pct(cur,base):
    return ((cur/base)-1)*100 if cur and base else None

def snapshot():
    ingest(); rows=recent_news(limit=20,max_age_hours=24)
    try: eth=_ticker('ETHUSDT'); btc=_ticker('BTCUSDT')
    except Exception: eth=btc=None
    items=[]
    for r in rows:
        age=max(0,time.time()-float(r['first_seen_ts'] or time.time()))
        er=_pct(eth,r.get('baseline_eth')); br=_pct(btc,r.get('baseline_btc'))
        reaction=0
        if er is not None:
            reaction += max(-45,min(45,er*45))
        if br is not None:
            reaction += max(-20,min(20,br*25))
        sem=int(r.get('semantic_bias') or 0)
        # First 90 seconds: headline only; afterwards market reaction dominates.
        weight=min(1.0,max(0.0,(age-90)/300))
        score=round((1-weight)*sem + weight*(0.25*sem+0.75*reaction))
        score=max(-100,min(100,score))
        bias='BULLISH' if score>=20 else ('BEARISH' if score<=-20 else 'NEUTRAL')
        confirm='WAITING' if age<90 else ('YES' if (sem==0 or score==0 or (sem>0 and score>0) or (sem<0 and score<0)) else 'NO')
        items.append({'event_id':r['event_id'],'source':r['source'],'title':r['title'],'url':r['url'],
                      'impact':r['impact'],'published_utc':datetime.fromtimestamp(float(r['published_ts']),timezone.utc).isoformat(timespec='seconds'),
                      'age_min':round(age/60,1),'semantic_bias':sem,'eth_reaction_pct':None if er is None else round(er,3),
                      'btc_reaction_pct':None if br is None else round(br,3),'eth_bias':bias,'score':score,'market_confirms':confirm})
    high=[x for x in items if x['impact']=='HIGH']
    focus=(high or items)[:1]
    f=focus[0] if focus else None
    return {'timestamp_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),'focus':f,'items':items[:10],
            'sources':[x[0] for x in FEEDS],
            'note':'Impact is headline classification plus observed ETH/BTC reaction after detection; it is not a forecast of the economic release.'}
