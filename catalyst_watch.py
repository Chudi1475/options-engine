"""Separate public catalyst watch ledger. Never places orders or enables entries.

Import dated public events with --import-events, then --report. All revisions
and rejected budget cases are preserved. A watch is not a prediction of direction.
"""
import argparse,hashlib,json,math,os
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal,ROUND_CEILING
from urllib.parse import urlparse
import config,storage_io
from grading_contract import timestamp
BUDGETS=(25,50,75,100)

def ledger_path():
    directory=config.DATA_DIR/'catalyst_data'
    directory.mkdir(parents=True,exist_ok=True)
    return directory/'watches.jsonl'
def read():
    try:return [json.loads(s) for s in ledger_path().read_text().splitlines() if s.strip()]
    except FileNotFoundError:return []

def record(event,now=None):
    now=timestamp(now or datetime.now(timezone.utc));e=dict(event)
    for k in ['symbol','event_type','source_url','event_start','event_end','thesis']:
        if not e.get(k):raise ValueError('missing '+k)
    if urlparse(e['source_url']).scheme not in ('http','https'):raise ValueError('public source URL required')
    pub=timestamp(e['published_at']) if e.get('published_at') else None
    if pub is None and not e.get('publication_time_reason'):raise ValueError('missing publication timestamp reason')
    start,end=map(timestamp,[e['event_start'],e['event_end']])
    if (pub is not None and pub>now) or start>end:raise ValueError('invalid event chronology')
    ident=e.get('event_id') or hashlib.sha256((e['symbol']+'|'+e['event_type']+'|'+e['source_url']).encode()).hexdigest()[:20]
    with storage_io.file_lock(ledger_path(),budget_s=1) as lock:
        if not lock.held:raise OSError('watch ledger lock unavailable')
        previous=[r for r in read() if r['event_id']==ident]
        e.update(event_id=ident,revision=len(previous)+1,discovered_at=now.isoformat(),first_discovered_at=previous[0]['first_discovered_at'] if previous else now.isoformat(),published_at=pub.isoformat() if pub else None,event_start=start.isoformat(),event_end=end.isoformat(),strategy_id='catalyst_watch',status='watching' if start>now else 'event_underway_or_past',actionable=False)
        if previous and all(e.get(k)==previous[-1].get(k) for k in ['symbol','event_type','source_url','event_start','event_end','thesis','published_at','publication_time_reason']):return previous[-1]
        res=storage_io.append_jsonl(ledger_path(),e)
        if not res.ok:raise OSError('watch write failed')
    return e

def budget_cases(contract,event,now,fees_round_trip):
    reasons=[]
    from feed_quality import quote_quality
    reasons+=quote_quality(contract,now)['reasons']
    for k in ['contract_id','expiry_date','last_trading_at_utc','multiplier','ask','tick_size']:
        if contract.get(k) is None:reasons.append('missing_'+k)
    try:
        if timestamp(contract['last_trading_at_utc'])<=timestamp(event['event_end']):reasons.append('expires_before_event_window_ends')
    except (KeyError,ValueError,TypeError):reasons.append('unverified_expiry')
    try:
        if isinstance(fees_round_trip,bool) or fees_round_trip is None or not math.isfinite(float(fees_round_trip)) or float(fees_round_trip)<0:reasons.append('unknown_fees')
    except (TypeError,ValueError,OverflowError):reasons.append('unknown_fees')
    for key in ('multiplier','tick_size','ask'):
        try:
            v=contract.get(key)
            if isinstance(v,bool) or not math.isfinite(float(v)) or float(v)<=0:reasons.append('invalid_'+key)
        except (TypeError,ValueError,OverflowError):reasons.append('invalid_'+key)
    if not reasons and Decimal(str(contract['ask'])) % Decimal(str(contract['tick_size'])) != 0:
        reasons.append('off_tick_ask')
    results=[]
    for budget in BUDGETS:
        cost=None
        if not reasons:
            raw=Decimal(str(contract['ask']))*Decimal(str(contract['multiplier']))+Decimal(str(fees_round_trip))
            cost=float(raw.quantize(Decimal('.01'),rounding=ROUND_CEILING))
        local=list(reasons)
        if cost is not None and cost>budget:local.append('over_budget')
        results.append({'budget':budget,'quantity':1 if not local else 0,'total_cost':cost,'feasible':not local,'reasons':local,'actionable':False})
    return results

def report():
    all_rows=read();latest={r['event_id']:r for r in all_rows}
    return {'strategy_id':'catalyst_watch','watches':len(latest),'revisions':len(all_rows),'entries_enabled':False,'events':list(latest.values())}

def main():
    p=argparse.ArgumentParser();p.add_argument('--import-events');p.add_argument('--refresh',action='store_true');p.add_argument('--report',action='store_true');p.add_argument('--output');a=p.parse_args()
    if a.refresh:refresh_from_result(discover_events())
    if a.import_events:
        events=json.loads(Path(a.import_events).read_text())
        for e in events:record(e)
    result=json.dumps(report(),indent=2)
    if a.output:Path(a.output).write_text(result)
    else:print(result)



def discover_events(symbols=None, now=None):
    """Fetch public headlines and calendar dates in the isolated provider process.

    Returned events are watches. Discovery never asserts a future direction.
    Errors are returned alongside events so an empty result is not called quiet.
    """
    import requests,xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from urllib.parse import quote
    from zoneinfo import ZoneInfo
    import news
    now=timestamp(now or datetime.now(timezone.utc));events=[];errors=[]
    if symbols is None:
        universe=list(dict.fromkeys(x.strip().upper() for x in os.environ.get('CATALYST_WATCH_SYMBOLS','LULU,DELL,NVDA,HOOD,AMD,AVGO,MU,HPE,NKE,DECK,IBKR,SOFI').split(',') if x.strip()))
        pages=max(1,(len(universe)+3)//4);page=int(now.timestamp()//3600)%pages
        symbols=universe[page*4:page*4+4]
    for symbol in symbols:
        try:
            url='https://feeds.finance.yahoo.com/rss/2.0/headline?s='+quote(symbol)
            response=requests.get(url,timeout=8);response.raise_for_status()
            for item in ET.fromstring(response.content).findall('.//item')[:8]:
                title=item.findtext('title') or '';link=item.findtext('link');pub=item.findtext('pubDate')
                if not link or not pub:continue
                at=parsedate_to_datetime(pub).astimezone(timezone.utc)
                if at>now or (now-at).total_seconds()>86400*3:continue
                events.append(dict(symbol=symbol,event_type='public_news',source_url=link,published_at=at.isoformat(),event_start=at.isoformat(),event_end=at.isoformat(),thesis=title))
        except Exception as e:errors.append({'symbol':symbol,'source':'public_rss','error':type(e).__name__})
        try:
            day=news.next_earnings(symbol)
            if day is not None and hasattr(day,'isoformat'):
                start=datetime(day.year,day.month,day.day,tzinfo=ZoneInfo('America/New_York'))
                end=start.replace(hour=23,minute=59,second=59)
                if end>now:
                    events.append(dict(event_id='earnings:'+symbol+':'+day.isoformat(),symbol=symbol,event_type='earnings_calendar',source_url='https://finance.yahoo.com/quote/'+symbol+'/calendar/',published_at=None,publication_time_reason='provider_calendar_has_no_publication_timestamp',event_start=start.isoformat(),event_end=end.isoformat(),time_confidence='date_only',thesis='Scheduled earnings date from provider calendar. Direction and exact release time unknown.'))
        except Exception as e:errors.append({'symbol':symbol,'source':'earnings_calendar','error':type(e).__name__})
    return {'events':events,'errors':errors,'checked_at':now.isoformat()}


def refresh_from_result(result):
    rows=[record(e) for e in result.get('events',[])]
    status={'checked_at':result.get('checked_at'),'events_received':len(rows),'errors':result.get('errors',[]),'entries_enabled':False}
    outcome=storage_io.write_json(ledger_path().parent/'refresh_status.json',status)
    if not outcome.ok:raise OSError('catalyst refresh status not saved')
    return status



def notice_events(now=None):
    now=timestamp(now or datetime.now(timezone.utc))
    events=[]
    for e in report()['events']:
        start,end=timestamp(e['event_start']),timestamp(e['event_end'])
        if e['event_type']=='earnings_calendar' and end>=now and (start-now).total_seconds()<=14*86400:
            events.append(e)
        elif e['event_type']=='public_news' and 0<=(now-start).total_seconds()<=6*3600:
            events.append(e)
    return sorted(events,key=lambda e:(e['event_type']!='earnings_calendar',e['event_start'],e['event_id']))[:4]

def watch_text(event):
    return (f"CATALYST WATCH: {event['symbol']}\n"
            f"{event['thesis'][:400]}\n"
            f"Window UTC: {event['event_start']} to {event['event_end']}\n"
            f"Source: {event['source_url']}\n"
            "Watch notice only. Direction and option return are unknown. "
            "No entry ticket has been approved. /catalysts shows the watch list.")

def summary_text():
    data=report()
    try:status=json.loads((ledger_path().parent/'refresh_status.json').read_text())
    except (OSError,ValueError):status={}
    lines=[f"Catalyst watches: {data['watches']}. Entry alerts are disabled.",
           f"Last collection: {status.get('checked_at') or 'not collected'}. Source errors: {len(status.get('errors',[]))}."]
    for e in notice_events():lines.append(f"{e['symbol']}: {e['thesis'][:160]} | {e['event_start']}")
    if not data['watches']:lines.append("No evidence has been collected. This does not mean no events exist.")
    return '\n'.join(lines)

def record_budget_evaluation(contract,event,now,fees_round_trip):
    row={'event_id':event['event_id'],'event_revision':event.get('revision'),
         'contract_id':contract.get('contract_id'),'evaluated_at':timestamp(now).isoformat(),
         'quote':dict(contract),'fees_round_trip':fees_round_trip,
         'cases':budget_cases(contract,event,now,fees_round_trip),'actionable':False}
    result=storage_io.append_jsonl(ledger_path().parent/'budget_evaluations.jsonl',row)
    if not result.ok:raise OSError('budget evaluation write failed')
    return row

if __name__=="__main__":main()
