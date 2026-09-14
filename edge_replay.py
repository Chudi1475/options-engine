"""Fixed prospective option comparison. No optimization and no invented fills.

Use --recorder-dir for saved observations or supply a JSON list of paired paths.
Every selected signal stays in the denominator. Quotes must have real quote
clocks. Receipt times and last trade times cannot substitute for quote times.
"""
import argparse
import json
import math
import random
import re
import statistics
from pathlib import Path
from grading_contract import timestamp
from feed_quality import quote_quality

POLICY = {'name':'current_momentum_two_contract_reference_v1','half_target':.25,
          'stop':.90,'runner_giveback':.40,'entry':'verified_ask',
          'exit':'observed_bid','default_quantity':2,'max_entry_latency_s':60,
          'max_pair_entry_difference_s':5,'max_sample_gap_s':60,
          'horizon_quote_tolerance_s':30,'fractional_contracts':False}

def missing(reason):
    return {'status':'missing','reason':reason}

def contract_identity(samples):
    ids={r.get('contract_id') for r in samples if r.get('contract_id')}
    return next(iter(ids)) if len(ids)==1 else None

def replay(samples,decision_at,horizon_at,fees_round_trip,quantity=2,max_latency_s=60):
    if type(quantity) is not int or quantity<=0:
        raise ValueError('positive integer quantity required')
    try:
        if isinstance(fees_round_trip,bool) or fees_round_trip is None or not math.isfinite(float(fees_round_trip)) or float(fees_round_trip)<0:
            return missing('unknown_fees')
        fees=float(fees_round_trip)
    except (ValueError,TypeError,OverflowError):return missing('unknown_fees')
    decision,end=timestamp(decision_at),timestamp(horizon_at)
    if end<=decision:raise ValueError('invalid horizon')
    if not contract_identity(samples):return missing('ambiguous_contract')
    valid=[]
    for row in samples:
        if not quote_quality(row)['eligible_for_execution_research']:continue
        at=timestamp(row['received_at_utc']);provider=timestamp(row['provider_at_utc'])
        if decision<=provider<=at<=end:valid.append(row)
    valid.sort(key=lambda r:timestamp(r['received_at_utc']))
    if not valid or (timestamp(valid[0]['received_at_utc'])-decision).total_seconds()>max_latency_s:
        return missing('no_timely_entry_quote')
    first=valid[0];ask=float(first['ask']);premium=ask*100*quantity
    if premium<=0:return missing('invalid_entry_premium')
    remaining=quantity;cash=0.;peak=0.;half_done=False;legs=[];previous=timestamp(first['received_at_utc'])
    for row in valid:
        at=timestamp(row['received_at_utc'])
        if (at-previous).total_seconds()>POLICY['max_sample_gap_s']:
            return missing('quote_path_gap_before_exit')
        previous=at
        bid=float(row['bid']);ret=bid/ask-1;peak=max(peak,ret)
        count=0;reason=None
        if ret<=-POLICY['stop']:count=remaining;reason='stop'
        elif not half_done and ret>=POLICY['half_target']:
            count=quantity//2 if quantity>1 else 1
            half_done=True;reason='half_target' if quantity>1 else 'single_contract_reference_target'
        elif half_done and ret<=peak-POLICY['runner_giveback']:
            count=remaining;reason='runner_giveback'
        if count:
            remaining-=count;cash+=count*100*bid
            legs.append({'quantity':count,'bid':bid,'at':row['received_at_utc'],'reason':reason})
        if not remaining:break
    if remaining:
        last=valid[-1]
        # Never use a quote received AFTER the declared horizon. The last
        # pre-horizon quote is a labeled sampled approximation, not a fill.
        if (end-timestamp(last['received_at_utc'])).total_seconds()>POLICY['horizon_quote_tolerance_s']:
            return missing('missing_terminal_quote')
        cash+=remaining*100*float(last['bid'])
        legs.append({'quantity':remaining,'bid':float(last['bid']),'at':last['received_at_utc'],'reason':'horizon_last_observed'})
    net=cash-premium-fees
    return {'status':'graded','net_dollars':net,'net_return_pct':100*net/premium,
            'premium_dollars':premium,'exit_reason':legs[-1]['reason'],'legs':legs,
            'entry_at':first['received_at_utc'],'exit_at':legs[-1]['at'],
            'execution_verified':False,'quantity':quantity,
            'matches_current_fractional_allocation':quantity%2==0,
            'basis':'sampled_ask_entry_bid_exit_simulation_with_assumed_standard_multiplier'}

def _opposite_verified(a,b):
    if not a or not b or a==b:return False
    pattern=r'(.+?)(\d{6})([CP])(\d{8})'
    x,y=re.fullmatch(pattern,a),re.fullmatch(pattern,b)
    return bool(x and y and x[1]==y[1] and x[2]==y[2] and x[3]!=y[3])

def compare(pairs,seed=917):
    rows=[];seen=set()
    for p in pairs:
        key=p['candidate_id']
        if key in seen:raise ValueError('duplicate candidate')
        seen.add(key)
        try:
            kw={k:p.get(k) for k in ('decision_at','horizon_at','fees_round_trip')}
            kw['quantity']=p.get('quantity',2)
            reason=p.get('exclusion_reason')
            if not reason and not _opposite_verified(contract_identity(p.get('chosen',[])),contract_identity(p.get('opposite',[]))):
                reason='unverified_opposite_contract'
            if reason:a=b=missing(reason)
            else:
                a=replay(p['chosen'],**kw);b=replay(p['opposite'],**kw)
                if a['status']==b['status']=='graded' and abs((timestamp(a['entry_at'])-timestamp(b['entry_at'])).total_seconds())>POLICY['max_pair_entry_difference_s']:
                    a=b=missing('unmatched_entry_times')
        except (KeyError,ValueError,TypeError,OverflowError):a=b=missing('invalid_pair_schema')
        row={'candidate_id':key,'cluster_id':p.get('cluster_id') or p.get('session_date') or 'unknown_cluster','chosen':a,'opposite':b}
        if a['status']==b['status']=='graded':
            row.update(random_direction_pct=(a['net_return_pct']+b['net_return_pct'])/2,
                       advantage_pct=(a['net_return_pct']-b['net_return_pct'])/2)
        rows.append(row)
    complete=[r for r in rows if 'advantage_pct' in r];clusters={}
    for r in complete:clusters.setdefault(r['cluster_id'],[]).append(r)
    boot=[];rng=random.Random(seed);groups=list(clusters.values())
    if len(groups)>=2:
        for _ in range(2000):
            drawn=[r for g in rng.choices(groups,k=len(groups)) for r in g]
            boot.append(statistics.mean(r['advantage_pct'] for r in drawn))
    boot.sort()
    return {'policy':POLICY,'opportunities':len(rows),'paired_complete':len(complete),
            'missing':len(rows)-len(complete),'independent_clusters':len(groups),
            'chosen_mean_net_return_pct':statistics.mean(r['chosen']['net_return_pct'] for r in complete) if complete else None,
            'mean_advantage_pct':statistics.mean(r['advantage_pct'] for r in complete) if complete else None,
            'bootstrap_interval_95':[boot[50],boot[1949]] if boot else None,'seed':seed,
            'status':'inconclusive' if not complete else 'research_only_not_promotion',
            'note':'Same-session clusters. Sampled quotes, not fills. Requires absolute net edge, declared sample size, coverage and risk criteria. No historical tuning. Fees are total round trip per position, not per contract. Odd quantities are a separate allocation policy.',
            'rows':rows}

def read_rows(directory,kind):
    rows=[]
    for path in sorted(Path(directory).glob(kind+'-*.jsonl')):
        for number,line in enumerate(path.read_text().splitlines(),1):
            if not line.strip():continue
            try:rows.append(json.loads(line))
            except ValueError:raise ValueError(f'Unreadable {kind} row {number}; denominator cannot be trusted')
    return rows

def recorder_pairs(directory,fees_round_trip=None):
    candidates={}
    for r in read_rows(directory,'candidates'):
        if r.get('strategy_id')=='momentum':candidates[r['candidate_id']]=r
    for signal in read_rows(Path(directory).parent/'fills','signals'):
        if signal.get('contract_id') and signal.get('candidate_id') not in candidates:
            cid=signal.get('candidate_id') or 'missing_candidate:'+signal['position_id']
            candidates[cid]={'candidate_id':cid,'selected':True,'source_missing':True,
                             'decision_at_utc':signal.get('alerted_at_utc'),'session_date':None}
    samples=read_rows(directory,'samples');events=read_rows(directory,'events')
    pairs=[]
    for cid,c in candidates.items():
        if not c.get('selected'):continue
        chosen=[s for s in samples if s.get('candidate_id')==cid and s.get('contract_role')=='chosen']
        opposite=[s for s in samples if s.get('candidate_id')==cid and s.get('contract_role')=='direction_control']
        horizons={s.get('observation_end_utc') for s in chosen+opposite if s.get('observation_end_utc')}
        controls=[e for e in events if e.get('candidate_id')==cid and e.get('event_type')=='control_selected' and e.get('contract_role')=='direction_control']
        reason=None
        if not controls:reason='missing_control_selection_provenance'
        else:
            try:
                last=controls[-1]
                if (timestamp(last['grid_read_at_utc'])-timestamp(c['decision_at_utc'])).total_seconds()>60:
                    reason='control_grid_observed_too_late'
            except (ValueError,KeyError,TypeError):reason='unknown_control_grid_time'
        if len(horizons)!=1:reason='missing_or_mixed_horizon'
        if c.get('source_missing'):reason='signal_missing_candidate_record'
        pairs.append({'candidate_id':cid,'cluster_id':c.get('session_date') or 'unknown_session',
                      'decision_at':c.get('decision_at_utc'),'horizon_at':next(iter(horizons)) if len(horizons)==1 else None,
                      'fees_round_trip':fees_round_trip,'quantity':2,'chosen':chosen,'opposite':opposite,'exclusion_reason':reason})
    return pairs

def main():
    parser=argparse.ArgumentParser();parser.add_argument('input',nargs='?');parser.add_argument('--recorder-dir')
    parser.add_argument('--fees-round-trip',type=float);parser.add_argument('--output',required=True);args=parser.parse_args()
    if bool(args.input)==bool(args.recorder_dir):parser.error('choose one input JSON or --recorder-dir')
    pairs=recorder_pairs(args.recorder_dir,args.fees_round_trip) if args.recorder_dir else json.loads(Path(args.input).read_text())
    result=compare(pairs);Path(args.output).write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
if __name__=='__main__':main()
