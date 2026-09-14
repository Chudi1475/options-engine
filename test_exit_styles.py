"""Offline regression of the existing exit policy. No data download or tuning."""
import os,tempfile,unittest
os.environ['BOT_TEST_MODE']='1'
os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='kelbot_exit_fixture_')
from datetime import datetime
from zoneinfo import ZoneInfo
import positions,config

class ExistingExitPolicyTests(unittest.TestCase):
 def position(self):
  return positions.Position(id='fixture',date='2026-09-09',time_et='10:00:00',ticker='SPY',direction='call',right='C',strike=640,expiry='2026-09-11',entry_mid=1,entry_source='quote')
 def step(self,p,pct,comparable=True):
  return positions.step(p,datetime(2026,9,9,10,5,tzinfo=ZoneInfo('America/New_York')),1+pct/100,'quote',None,False,positions.DEFAULT_OLD_BRACKET,comparable)
 def test_half_then_trail_keeps_weighted_result(self):
  p=self.position();half=config.TP_HALF_PCT+1;peak=half+50;end=peak-config.RUNNER_GIVEBACK_PCT
  self.assertEqual(self.step(p,half)[0]['type'],'sell_half')
  self.step(p,peak);self.assertEqual(p.state,'half_sold')
  self.assertEqual(self.step(p,end)[0]['type'],'runner_trail')
  self.assertAlmostEqual(p.final_pnl_pct,(half+end)/2,places=2)
 def test_observed_stop_gap_is_not_clipped_to_threshold(self):
  p=self.position();observed=max(-100,config.STOP_PCT-5)
  self.assertEqual(self.step(p,observed)[0]['type'],'stop')
  self.assertAlmostEqual(p.final_pnl_pct,observed,places=2)
 def test_incomparable_mark_does_not_take_half(self):
  p=self.position();events=self.step(p,config.TP_HALF_PCT+50,False)
  self.assertFalse(any(e['type']=='sell_half' for e in events));self.assertEqual(p.state,'open')
if __name__=='__main__':unittest.main()
