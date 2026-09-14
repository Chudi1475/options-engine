"""Observation quality checks. No eligibility or strategy thresholds move."""
from datetime import timedelta
import math
from grading_contract import timestamp

def quote_quality(row, now=None, max_age_s=60):
    reasons=[]
    bid,ask=row.get('bid'),row.get('ask')
    try:
        if any(isinstance(v,bool) or v is None or not math.isfinite(float(v)) or float(v)<0 for v in (bid,ask)):
            reasons.append('invalid_or_missing_quote')
        elif float(ask)<float(bid):reasons.append('crossed_quote')
    except (TypeError,ValueError,OverflowError):reasons.append('invalid_or_missing_quote')
    if row.get('is_model') is not False:reasons.append('not_verified_quote_basis')
    if 'indicative' in str(row.get('feed','')).lower():reasons.append('indicative_feed')
    try:
        provider=timestamp(row['provider_at_utc']);received=timestamp(row['received_at_utc'])
        if provider>received:reasons.append('future_quote')
        if (received-provider).total_seconds()>max_age_s:reasons.append('stale_quote')
        if now is not None and (timestamp(now)-provider).total_seconds()>max_age_s:reasons.append('stale_at_decision')
    except (KeyError,TypeError,ValueError):reasons.append('unverifiable_quote_time')
    return {'eligible_for_execution_research':not reasons,'reasons':reasons}

def bar_quality(frame, now, bar_minutes=5):
    reasons=[]
    if frame is None or frame.empty:return {'reasons':['no_bars'],'measurement_ready':False}
    try:
        ts=[timestamp(i) for i in frame.index]
        if len(set(ts))!=len(ts):reasons.append('duplicate_bars')
        if ts!=sorted(ts):reasons.append('unordered_bars')
        if any(t+timedelta(minutes=bar_minutes)>timestamp(now) for t in ts):reasons.append('incomplete_bars')
        for _,r in frame.iterrows():
            values=[float(r[k]) for k in ['Open','High','Low','Close']]
            if not all(math.isfinite(v) for v in values) or not values[2]<=min(values[0],values[3])<=max(values[0],values[3])<=values[1]:
                reasons.append('invalid_ohlc');break
    except (TypeError,ValueError,KeyError):reasons.append('invalid_bar_schema_or_time')
    return {'reasons':sorted(set(reasons)),'measurement_ready':not reasons,'feed':frame.attrs.get('source_feed','unspecified')}
