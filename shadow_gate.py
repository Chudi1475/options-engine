"""Additional research gate only. Existing live eligibility is preserved."""
import hashlib,json,math

def policy_hash(policy):
    return hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def evaluate(*, legacy_passed, report, expected_policy_hash, minimum_winrate=70, strict_raw=False):
    reasons=[]
    if not legacy_passed:reasons.append('legacy_gate_rejected')
    if not isinstance(report,dict):report={};reasons.append('missing_matching_report')
    if report.get('policy_hash')!=expected_policy_hash:reasons.append('policy_mismatch')
    wr=report.get('win_rate');ev=report.get('net_expectancy_pct')
    if not isinstance(wr,(float,int)) or not math.isfinite(wr) or not 0<=wr<=100:reasons.append('invalid_win_rate')
    elif (wr if strict_raw else round(wr))<minimum_winrate:reasons.append('win_rate_below_floor')
    if not isinstance(ev,(float,int)) or not math.isfinite(ev) or ev<=0:reasons.append('nonpositive_or_unknown_net_expectancy')
    if not report.get('measurement_complete') or report.get('trades',0)<=0:reasons.append('incomplete_evidence')
    return {'shadow_passed':not reasons,'legacy_passed':bool(legacy_passed),'disagreement':bool(legacy_passed) and bool(reasons),'reasons':reasons,'expected_policy_hash':expected_policy_hash,'report_id':report.get('report_id'),'raw_win_rate':wr,'strict_raw':strict_raw,'changes_live_gate':False}


def export(directory):
    from edge_replay import read_rows
    latest={}
    for row in read_rows(directory,'candidates'):
        if row.get('strategy_id')=='momentum':latest[row['candidate_id']]=row
    rows=[{'candidate_id':r['candidate_id'],'session_date':r.get('session_date'),
           'selected':r.get('selected'),'shadow_gate':r.get('shadow_gate')} for r in latest.values()]
    return {'candidate_opportunities':len(rows),'missing_shadow':sum(r['shadow_gate'] is None for r in rows),
            'disagreements':sum(bool((r['shadow_gate'] or {}).get('disagreement')) for r in rows),
            'changes_live_gate':False,'rows':rows}

if __name__=='__main__':
    import argparse
    from pathlib import Path
    p=argparse.ArgumentParser();p.add_argument('--recorder-dir',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    Path(a.output).write_text(json.dumps(export(a.recorder_dir),indent=2,allow_nan=False)+'\n')
