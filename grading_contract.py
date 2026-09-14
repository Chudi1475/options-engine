"""Pure research grading. Live stop and target rules are not changed here."""
from datetime import datetime, timedelta, timezone
import math
VERSION = 'research_session_v1'

def timestamp(value):
    t = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if t.tzinfo is None: raise ValueError('timezone required')
    return t.astimezone(timezone.utc)

def evaluate_bars(*, entry, stop, direction, targets, bars, entry_at, horizon_at, bar_minutes=5):
    """OHLC bars are open stamped. Partial entry bars cannot prove a touch.

    Ties use stop first with ambiguity reported. Gaps fill at the worse open.
    Missing terminal coverage stays missing instead of becoming a loss.
    """
    start, end = timestamp(entry_at), timestamp(horizon_at)
    sign = 1 if direction in ('BUY', 'call', 'C') else -1 if direction in ('SELL','put','P') else 0
    entry, stop = float(entry), float(stop)
    if not all(math.isfinite(v) for v in (entry, stop)) or not isinstance(bar_minutes, int) or bar_minutes <= 0:
        raise ValueError('finite prices and positive bar duration required')
    risk = abs(entry-stop)
    if not sign or not risk or sign*(float(entry)-float(stop)) <= 0 or end <= start:
        raise ValueError('invalid entry, stop, direction or horizon')
    tiers = {k:float(v) for k,v in targets.items() if v is not None}
    if not tiers: raise ValueError('at least one target required')
    if any(not math.isfinite(v) or sign*(v-entry)<=0 for v in tiers.values()):raise ValueError('invalid target')
    hit={k:None for k in tiers}; returns={k:None for k in tiers}; uncertain=[]; mfe=0.; stopped=False
    last_close=None;last_end=None; used=0; prior=None; expected=start; path_gap=False
    for b in sorted(bars,key=lambda x:timestamp(x['at'])):
        at=timestamp(b['at']);bend=at+timedelta(minutes=bar_minutes)
        if at < start or bend>end:continue
        if prior==at:raise ValueError('duplicate bar')
        if at != expected:
            path_gap=True
            break
        expected=bend
        prior=at
        hi,lo,close=float(b['high']),float(b['low']),float(b['close'])
        op=float(b.get('open',entry))
        if not all(math.isfinite(v) for v in (hi,lo,close,op)) or lo>hi or not lo<=close<=hi or not lo<=op<=hi:raise ValueError('invalid OHLC')
        used+=1;last_close=close;last_end=bend
        mfe=max(mfe,(hi-entry)/risk if sign>0 else (entry-lo)/risk)
        stop_hit=lo<=stop if sign>0 else hi>=stop
        for k,target in tiers.items():
            if hit[k] is not None:continue
            target_hit=hi>=target if sign>0 else lo<=target
            if stop_hit:
                hit[k]=False;returns[k]=min(-1., sign*(op-entry)/risk)
                if target_hit:uncertain.append({'tier':k,'at':at.isoformat(),'reason':'both_barriers_in_bar'})
            elif target_hit:hit[k]=True;returns[k]=sign*(target-entry)/risk
        if stop_hit:stopped=True
        if all(v is not None for v in hit.values()):break
    terminal_covered=last_end==end
    for k in tiers:
        if hit[k] is None and terminal_covered:returns[k]=sign*(last_close-entry)/risk
    complete=all(v is not None for v in returns.values()) and bool(used) and not path_gap
    return dict(grading_version=VERSION, hit=hit, returns_r=returns, stopped=stopped,mfe_r=round(mfe,3),terminal='stop' if stopped else 'target' if used and all(v is True for v in hit.values()) else 'horizon' if terminal_covered else 'missing', terminal_covered=terminal_covered,measurement_complete=complete, ambiguous=uncertain,path_gap=path_gap,bar_count=used,horizon_at=end.isoformat(),entry_at=start.isoformat(),price_basis='underlying_OHLC_research',execution_verified=False)

def frame_bars(frame):
    return [dict(at=i.to_pydatetime() if hasattr(i,'to_pydatetime') else i,open=r.get('Open',r['Close']),high=r['High'],low=r['Low'],close=r['Close']) for i,r in frame.iterrows()]
