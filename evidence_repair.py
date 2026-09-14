"""Recover missing evidence links from saved intents without sending messages.

Previously unobserved prices remain missing. Recovery cannot reconstruct quotes.
"""
from datetime import datetime,timezone
import fill_journal as fills
import trade_recorder as recorder
import event_journal
from grading_contract import timestamp

def repair(now=None):
    now=timestamp(now or datetime.now(timezone.utc));count=0;errors=[]
    for intent in event_journal.all_intents():
        if intent.kind not in ('entry','sniper_entry') or not intent.position_id:continue
        payload=intent.payload or {};pos=payload.get('position') or {}
        symbol=pos.get('ticker') or payload.get('symbol') or ''
        contract=''
        if intent.kind=='entry' and all(pos.get(k) for k in ('ticker','right','strike','expiry')):
            contract=recorder.contract_id_for(pos['ticker'],pos['right'],pos['strike'],pos['expiry'])
        try:
            fills.note_signal(intent.position_id,candidate_id=intent.candidate_id,contract_id=contract,
                              symbol=symbol,recipients=[d.recipient_ref for d in intent.deliveries()],
                              alerted_at_utc=intent.decided_at_utc)
            count+=1
            # Only recover a still-open observation window. A missing historic
            # price path remains missing and is visible to replay coverage.
            if contract and intent.candidate_id:
                date=pos.get('date')
                end=recorder.common_horizon_utc(date)
                if timestamp(end)>now and not recorder.chosen_observation(intent.position_id):
                    recorder.register_contract(underlying=pos['ticker'],option_right=pos['right'],strike=pos['strike'],expiry_date=pos['expiry'])
                    recorder.observe(contract_id=contract,contract_role=recorder.CHOSEN,candidate_id=intent.candidate_id,position_id=intent.position_id,
                                     observation_end_utc=end,underlying=symbol,right=pos['right'],strike=pos['strike'],expiry_date=pos['expiry'])
                    recorder.request_controls(candidate_id=intent.candidate_id,position_id=intent.position_id,
                        underlying=symbol,right=pos['right'],strike=pos['strike'],spot=pos.get('spot_at_signal'),
                        expiry_date=pos['expiry'],observation_end_utc=end,decision_at_utc=intent.decided_at_utc)
                    recorder._record_gap(intent.decided_at_utc,now.isoformat(),'recovered observation; earlier quotes unavailable')
        except Exception as exc:errors.append({'intent_id':intent.journal_id,'error':type(exc).__name__})
    return {'signals_checked':count,'errors':errors,'reconstructs_historical_prices':False}
