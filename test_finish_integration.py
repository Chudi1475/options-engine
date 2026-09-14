"""Offline integration checks for the completion release. No live recipients."""
import os,tempfile
os.environ['BOT_TEST_MODE']='1'
os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='kelbot_finish_')
import unittest,json,threading,time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import config,trade_recorder as tr,fill_journal as fj,quotes,scanner,event_journal as ej
import catalyst_watch as cw
from edge_replay import compare,recorder_pairs,replay
from feed_quality import quote_quality
from grading_contract import evaluate_bars

def stamp(m=0):return f'2026-09-09T14:{m:02d}:00+00:00'
def q(m,bid,ask,right='C'):
 return dict(provider_at_utc=stamp(m),received_at_utc=stamp(m),is_model=False,feed='opra',bid=bid,ask=ask,contract_id=f'SPY260911{right}00640000')
def blocked_worker(pipe):
 try:pipe.recv();pipe.recv()
 except EOFError:pass
 finally:pipe.close()
def echo_worker(pipe):
 try:
  while True:pipe.send((True,{'request':pipe.recv()}))
 except EOFError:pass
 finally:pipe.close()

class FinishTests(unittest.TestCase):
 def setUp(self):
  config.DATA_DIR=Path(tempfile.mkdtemp(prefix='kelbot_case_'))
  tr._reset_for_test();fj._reset_for_test()
 def tearDown(self):tr.stop_sampler();tr.stop()
 def test_invalid_quote_never_raises_or_becomes_eligible(self):
  for bad in ('bad',float('nan'),float('inf'),True,-1):
   row=q(0,bad,1)
   self.assertFalse(quote_quality(row)['eligible_for_execution_research'])
 def test_quote_provider_clock_required(self):
  row=q(0,1,1.1);row['provider_at_utc']=None
  self.assertFalse(quote_quality(row)['eligible_for_execution_research'])
 def test_child_provider_deadline_and_restart(self):
  worker=quotes.ChainProcess(timeout=.2,worker=blocked_worker)
  start=time.monotonic()
  with self.assertRaises(TimeoutError):worker.read('SPY','2026-09-11')
  self.assertIsNone(worker.process);self.assertLess(time.monotonic()-start,4)
  worker.worker=echo_worker;worker.timeout=5
  self.assertEqual(worker.read('SPY','2026-09-11')['request'][0],'SPY');worker.close()
 def test_stuck_writer_not_replaced_or_double_drained(self):
  class Stuck:
   def is_alive(self):return True
   def join(self,timeout):pass
  t=Stuck();tr._WRITER[0]=t
  with patch.object(tr,'drain_once') as drain:
   self.assertFalse(tr.stop(timeout=.01));drain.assert_not_called()
  self.assertIs(tr._WRITER[0],t);tr._WRITER[0]=None
 def test_unknown_legacy_fees_stay_unknown(self):
  a=fj.record_fill('u','p',quantity=1,fill_price=1,fees=0)
  b=fj.record_fill('u','p',buy_or_sell='sell',quantity=1,fill_price=2,fees=0)
  self.assertEqual(fj._user_reconciliation([a,b])['round_trip_cash_cents'],10000)
  del a['fees_reason']
  self.assertIsNone(fj._user_reconciliation([a,b])['round_trip_cash_cents'])
 def test_bad_fill_values_rejected(self):
  for kw in ({'quantity':True},{'fill_price':float('nan')},{'fees':-1},{'multiplier':0}):
   args=dict(quantity=1,fill_price=1,fees=0);args.update(kw)
   with self.assertRaises(ValueError):fj.record_fill('u','p',**args)
 def test_signal_repair_is_idempotent(self):
  a=fj.note_signal('p',alerted_at_utc=stamp());b=fj.note_signal('p',alerted_at_utc=stamp())
  self.assertEqual(a['signal_id'],b['signal_id']);self.assertEqual(len(fj.read_records('signals')),1)
 def test_scanner_commit_joins_candidate(self):
  service=scanner.Service.__new__(scanner.Service);service.dry=False;service.backtest_new=None
  ctx={'policy_hash':'p','strategy_version':'v','source_commit':'c','deployment_id':'d','input_feed':'fixture'}
  now=datetime.fromisoformat(stamp())
  with patch.object(service,'recorder_context',return_value=ctx):
   cid=service.record_candidate('SPY','call',stamp(),[],{'momentum':1},now,gate_passed=True)
   chosen=service.record_candidate('SPY','call',stamp(),[],{'momentum':1},now,selected=True,position_id='p',gate_passed=True,candidate_id=cid,decision_id='decision')
  tr.drain_once();rows=tr.read_records('candidates')
  self.assertEqual(chosen,cid);self.assertEqual(rows[-1]['decision_id'],'decision');self.assertEqual(len({r['candidate_id'] for r in rows}),1)
 def test_invalid_provider_records_missing_path(self):
  tr.observe(contract_id='SPY260911C00640000',contract_role='chosen',candidate_id='c',position_id='p',observation_end_utc=stamp(5),underlying='SPY',right='C',strike=640,expiry_date='2026-09-11')
  tr.sample_once(lambda *a,**k:None,now=datetime.fromisoformat(stamp()));tr.drain_once()
  row=tr.read_records('samples')[-1];self.assertIsNone(row['received_at_utc']);self.assertIn('chain_read_failed',row['missing_reason'])
 def test_missing_bar_before_target_cannot_be_graded(self):
  r=evaluate_bars(entry=100,stop=99,direction='BUY',targets={'t':101},entry_at=stamp(),horizon_at=stamp(10),bars=[dict(at=stamp(5),open=100,high=102,low=100,close=101)])
  self.assertFalse(r['measurement_complete']);self.assertTrue(r['path_gap'])
 def test_bad_grading_inputs_rejected(self):
  args=dict(entry=100,stop=99,direction='BUY',targets={'t':101},entry_at=stamp(),horizon_at=stamp(5),bars=[])
  for kw in ({'entry':float('nan')},{'targets':{}},{'bar_minutes':0}):
   with self.assertRaises(ValueError):evaluate_bars(**dict(args,**kw))
 def test_replay_respects_half_and_runner(self):
  r=replay([q(0,1,1),q(1,1.3,1.4),q(2,1.8,1.9),q(3,1.39,1.49)],stamp(),stamp(3),.20)
  self.assertEqual(r['status'],'graded');self.assertEqual([x['quantity'] for x in r['legs']],[1,1]);self.assertEqual(r['exit_reason'],'runner_giveback');self.assertAlmostEqual(r['net_dollars'],68.8)
 def test_replay_gap_and_unknown_fees_excluded(self):
  self.assertEqual(replay([q(0,1,1),q(5,2,2.1)],stamp(),stamp(5),0)['reason'],'quote_path_gap_before_exit')
  self.assertEqual(replay([q(0,1,1)],stamp(),stamp(1),None)['reason'],'unknown_fees')
 def test_opposite_cannot_be_same_contract(self):
  pair=dict(candidate_id='c',cluster_id='day',decision_at=stamp(),horizon_at=stamp(1),fees_round_trip=0,chosen=[q(0,1,1),q(1,2,2.1)],opposite=[q(0,1,1),q(1,2,2.1)])
  self.assertEqual(compare([pair])['paired_complete'],0)
 def test_rejected_signals_and_missing_controls_retained(self):
  args=dict(strategy_id='momentum',symbol='SPY',direction='call',session_date='2026-09-09',decision_at_utc=stamp(),observed_at_utc=stamp(),input_bar_end_utc=stamp(),input_feed='fixture',input_values={},gate_passed=True,reject_codes=[],selected=True,decision_id='d')
  tr.record_candidate(**args);tr.drain_once()
  pairs=recorder_pairs(tr.recorder_dir(),0)
  self.assertEqual(len(pairs),1);self.assertEqual(compare(pairs)['missing'],1)
 def test_budget_rejects_negative_and_unknown_inputs(self):
  event={'event_end':stamp(1)};row=q(0,.24,.25);row.update(expiry_date='2026-09-11',last_trading_at_utc=stamp(5),multiplier=-100,tick_size=.01)
  self.assertTrue(all(not x['feasible'] for x in cw.budget_cases(row,event,stamp(),0)))
 def test_watch_delivery_resolves_owner_not_broadcast_index(self):
  from types import SimpleNamespace
  service=scanner.Service.__new__(scanner.Service);service.dry=False
  intent=ej.Intent(journal_id='fixture',kind='catalyst_watch',text='Watch only',recipients=[{'recipient_index':0,'recipient_ref':ej.recipient_ref('owner')}])
  with patch.object(scanner.telegram,'owner_ids',return_value=['owner']),patch.object(scanner.telegram,'send_to_detailed',return_value={'status':'confirmed','message_id':1}) as send,patch.object(scanner.telegram,'send_detailed') as broadcast,patch.object(ej,'record_attempt'),patch.object(ej,'record_result'),patch.object(scanner,'log_alert'):
   service._deliver(intent,None);self.assertEqual(send.call_args.args[0],'owner');broadcast.assert_not_called()
 def test_watch_dedup_revision(self):
  event=dict(symbol='DELL',event_type='earnings_calendar',source_url='https://example.com/earnings',published_at=stamp(),event_start=stamp(1),event_end=stamp(5),thesis='Direction unknown')
  a=cw.record(event,now=stamp());b=cw.record(event,now=stamp())
  self.assertEqual(a,b);self.assertEqual(len(cw.notice_events(stamp())),1)
  self.assertFalse(a['actionable'])
 def test_daemon_runs_grading_after_session_with_learning_disabled(self):
  import assistant
  from unittest.mock import Mock
  from zoneinfo import ZoneInfo
  svc=scanner.Service.__new__(scanner.Service)
  for method in ('_enter_starting','maybe_catalyst_watch','start_sniper_watch','flush_pending','maybe_recap','maybe_request_digest','maybe_weekly','health_eod','maybe_learn','maybe_holiday_notice'):
   setattr(svc,method,Mock())
  svc.ensure_active=Mock(return_value=True);svc.maybe_grade_forward=Mock()
  svc.handle_commands=Mock(side_effect=SystemExit)
  with patch.object(scanner,'et_now',return_value=datetime(2026,9,9,17,0,tzinfo=ZoneInfo('America/New_York'))),patch.object(assistant,'probe_billing_async'),patch.object(assistant,'check_cooldown_recovery'),patch.dict(os.environ,{'LEARN_ENABLED':'false'}):
   with self.assertRaises(SystemExit):svc.daemon()
  svc.maybe_grade_forward.assert_called_once()
 def test_partition_mismatch_cannot_complete_job(self):
  import forward_ledger as fl
  with patch.object(fl,'_audit'):
   res=fl._grading_result(eligible=1,graded=1,statuses={})
  self.assertFalse(res['job_complete']);self.assertFalse(res['measurement_complete'])
 def test_repair_includes_resolved_sniper_intent(self):
  import evidence_repair
  intent=ej.Intent(journal_id='j',kind='sniper_entry',candidate_id='c',decision_id='d',position_id='p',decided_at_utc=stamp(),resolved=True,payload={'symbol':'SPY'})
  with patch.object(ej,'all_intents',return_value=[intent]):
   self.assertFalse(evidence_repair.repair(stamp(5))['errors']);evidence_repair.repair(stamp(5))
  signals=fj.read_records('signals');self.assertEqual(len(signals),1);self.assertEqual(signals[0]['candidate_id'],'c')
 def test_queue_loss_has_external_cumulative_log(self):
  import queue
  with patch.object(tr,'_Q',queue.Queue(maxsize=1)),patch('builtins.print') as log:
   tr._enqueue('health',{});self.assertFalse(tr._enqueue('health',{}))
  self.assertTrue(log.call_args.kwargs['flush']);self.assertIn('1 records lost',log.call_args.args[0])
 def test_no_bar_rejects_do_not_collapse(self):
  args=dict(strategy_id='momentum',symbol='SPY',direction=None,session_date='2026-09-09',decision_at_utc=stamp(),observed_at_utc=stamp(),input_bar_end_utc=None,input_feed='fixture',input_values={},gate_passed=False,selected=False)
  a=tr.record_candidate(**args,reject_codes=['no_bars']);b=tr.record_candidate(**args,reject_codes=['data_error'])
  self.assertNotEqual(a,b)

 def test_historical_deployment_id_not_used_as_runtime_identity(self):
  from types import SimpleNamespace
  svc=scanner.Service.__new__(scanner.Service)
  svc.cfg=SimpleNamespace(entry_start='09:45',entry_end='10:30',watchlist={'SPY':'SPY'})
  svc.old_bracket={};svc.feed=SimpleNamespace(backend_for=lambda symbol:'fixture')
  result=SimpleNamespace(usable=True,value={'source':{'commit':'source'},'deployed':{'deployment_id':'historical'}})
  with patch.object(scanner.storage_io,'read_json',return_value=result),patch.dict(os.environ,{'RAILWAY_DEPLOYMENT_ID':''}):
   self.assertEqual(svc.recorder_context()['deployment_id'],'')

if __name__=='__main__':unittest.main()
