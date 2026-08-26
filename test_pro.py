from macro_news import classify
from radar import classify_direction

def test_news_high_fomc():
    impact,bias=classify('Federal Reserve issues FOMC statement on federal funds rate')
    assert impact=='HIGH'

def test_news_bull_cut():
    impact,bias=classify('Federal Reserve announces rate cut')
    assert bias>0

def test_news_bear_hike():
    impact,bias=classify('Federal Reserve announces rate hike')
    assert bias<0

def test_direction_binary():
    assert classify_direction(55,30)=='LONG'
    assert classify_direction(25,70)=='SHORT'

from signal_priority import priority_from_scores, build_priority_state, priority_change

def test_priority_gap_exactly_five():
    assert priority_from_scores(35,30)=='LONG'
    assert priority_from_scores(30,35)=='SHORT'

def test_priority_gap_below_five_no_alert_state():
    assert priority_from_scores(34,30) is None
    assert priority_from_scores(30,34) is None

def test_priority_change_and_elapsed():
    old=build_priority_state(35,30,ts=1000)
    new=build_priority_state(30,35,ts=1900)
    c=priority_change(old,new)
    assert c['from']=='LONG' and c['to']=='SHORT' and c['elapsed_min']==15
