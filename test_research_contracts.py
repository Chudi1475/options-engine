import os,tempfile
os.environ['BOT_TEST_MODE']='1';os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='kelbot_research_')
import unittest
from datetime import datetime,timezone,timedelta
from grading_contract import evaluate_bars
from shadow_gate import evaluate
from edge_replay import compare
import catalyst_watch as cw

def stamp(m):return f'2026-09-09T14:{m:02d}:00+00:00'
def quote(m,bid,ask,cid):
 return dict(provider_at_utc=stamp(m),received_at_utc=stamp(m),bid=bid,ask=ask,is_model=False,feed='opra',contract_id=cid)
class ResearchTests(unittest.TestCase):
 def test_bar_tie_and_gap(self):
  r=evaluate_bars(entry=100,stop=99,direction='BUY',targets={'t04':100.4},bars=[dict(at=stamp(0),open=98,high=101,low=97,close=100)],entry_at=stamp(0),horizon_at=stamp(5))
  self.assertEqual(r['returns_r']['t04'],-2);self.assertTrue(r['ambiguous'])
 def test_partial_bar_cannot_create_win(self):
  r=evaluate_bars(entry=100,stop=99,direction='BUY',targets={'t04':100.4},bars=[dict(at=stamp(0),open=100,high=101,low=100,close=100.1)],entry_at=stamp(1),horizon_at=stamp(5))
  self.assertEqual(r['terminal'],'missing');self.assertIsNone(r['hit']['t04'])
 def test_terminal_return_is_not_invented_stop(self):
  r=evaluate_bars(entry=100,stop=99,direction='BUY',targets={'t1':101},bars=[dict(at=stamp(0),open=100,high=100.3,low=99.8,close=100.2)],entry_at=stamp(0),horizon_at=stamp(5))
  self.assertAlmostEqual(r['returns_r']['t1'],.2);self.assertIsNone(r['hit']['t1'])
 def test_shared_adapter(self):
  import sniper_book
  args=dict(entry=100,stop=99,direction='BUY',targets={'t':101},bars=[dict(at=stamp(0),open=100,high=102,low=100,close=101)],entry_at=stamp(0),horizon_at=stamp(5))
  self.assertEqual(evaluate_bars(**args),sniper_book.research_grade(**args))
 def test_shadow_cannot_admit_legacy_reject(self):
  r=evaluate(legacy_passed=False,report={'policy_hash':'x','trades':30,'win_rate':90,'net_expectancy_pct':20,'measurement_complete':True},expected_policy_hash='x')
  self.assertFalse(r['shadow_passed'])
 def test_missing_report_fails_shadow(self):
  self.assertFalse(evaluate(legacy_passed=True,report=None,expected_policy_hash='x')['shadow_passed'])
 def test_direction_control_uses_actual_opposite(self):
  pair=dict(candidate_id='c',cluster_id='day',decision_at=stamp(0),horizon_at=stamp(1),fees_round_trip=.1,chosen=[quote(0,1,1.1,'SPY260909C00640000'),quote(1,1.5,1.6,'SPY260909C00640000')],opposite=[quote(0,1,1.1,'SPY260909P00640000'),quote(1,.1,.2,'SPY260909P00640000')])
  r=compare([pair]);self.assertEqual(r['paired_complete'],1);self.assertGreater(r['rows'][0]['advantage_pct'],0)
  pair['opposite'][0]['provider_at_utc']=None
  self.assertEqual(compare([pair])['paired_complete'],0)
 def test_watch_revision_and_budget(self):
  now=stamp(0);ev=dict(symbol='DELL',event_type='earnings',source_url='https://example.com/event',published_at=stamp(0),event_start=stamp(1),event_end=stamp(2),thesis='Scheduled report, direction unknown')
  a=cw.record(ev,now=now);self.assertEqual(cw.record(ev,now=now)['revision'],1)
  ev['event_end']=stamp(3);self.assertEqual(cw.record(ev,now=now)['revision'],2)
  q=quote(0,.24,.25,'DELL');q.update(last_trading_at_utc=stamp(5),multiplier=100,tick_size=.01,expiry_date='2026-09-09')
  cases=cw.budget_cases(q,ev,now,.1);self.assertFalse(cases[0]['feasible']);self.assertTrue(cases[1]['feasible'])
  q['last_trading_at_utc']=stamp(2);self.assertTrue(all(not x['feasible'] for x in cw.budget_cases(q,ev,now,.1)))
 def test_catalyst_news_read_sends_the_shared_browser_header(self):
  # yahoo answers 429 to a feed request with no browser style user agent
  import news,requests
  from unittest.mock import patch
  seen=[]
  class Resp:
   content=b'<rss><channel></channel></rss>'
   def raise_for_status(self):pass
  def fake_get(url,timeout=None,headers=None):
   seen.append(headers or {});return Resp()
  with patch.object(requests,'get',fake_get),patch.object(news,'next_earnings',lambda s:None):
   out=cw.discover_events(symbols=['AMD'])
  self.assertEqual(out['errors'],[])
  self.assertEqual([h.get('User-Agent') for h in seen],[news.USER_AGENT])

if __name__=='__main__':unittest.main()
