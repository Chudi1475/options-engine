"""Independent regressions for the unfinished September source snapshot."""
import os
import tempfile
os.environ['BOT_TEST_MODE'] = '1'
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='kelbot_completion_')
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import trade_recorder as tr
import fill_journal as fj
import forward_ledger as fl
import storage_io

class CompletionTests(unittest.TestCase):
    def setUp(self):
        tr._reset_for_test()
        fj._reset_for_test()

    def sample(self, **kw):
        args=dict(candidate_id='c1',contract_id='SPY260909C00640000',contract_role='chosen',provider='yfinance',feed='yahoo option chain',provider_at_utc=None,received_at_utc='2026-09-09T14:00:00+00:00',requested_at_utc='2026-09-09T13:59:59+00:00',bid=1.,ask=1.1,bid_size=None,ask_size=None,underlying_price=None,underlying_at_utc=None,price_basis='quote_mid',is_model=False,quality_flags=[],observation_end_utc='2026-09-09T20:00:00+00:00')
        args.update(kw);tr.record_sample(**args);tr.drain_once()

    def test_unknown_fees_are_not_net(self):
        a=fj.record_fill('u','p',buy_or_sell=fj.BUY,quantity=1,fill_price=1)
        fj.record_fill('u','p',buy_or_sell=fj.SELL,quantity=1,fill_price=2)
        self.assertIsNone(a['fees'])
        self.assertIsNone(fj.reconcile('p')['by_user']['u']['round_trip_cash_cents'])

    def test_coverage_survives_restart(self):
        tr._bump('dropped_samples',25);tr._persist_coverage(force=True)
        tr._reset_for_test(keep_files=True)
        self.assertEqual(tr.coverage()['dropped_samples'],25)

    def test_registry_write_failure_visible(self):
        with patch.object(storage_io,'write_json',return_value=type('R',(),{'ok':False,'status':'failed'})()):
            tr._save_observations()
        self.assertGreater(tr.coverage()['write_failures'],0)

    def test_identical_unknown_timestamp_not_fresh_evidence(self):
        self.sample();self.sample()
        self.assertIn('unchanged_without_timestamp',tr.read_records('samples')[-1]['quality_flags'])

    def test_missing_underlying_explained(self):
        self.sample()
        self.assertIn('no_underlying_price',tr.read_records('samples')[-1]['missing_reason'])

    def test_model_does_not_become_observed_quote_high(self):
        self.sample();self.sample(bid=None,ask=None,mark=9.99,is_model=True,price_basis='black_scholes_estimate')
        p=tr.path_summary('c1','SPY260909C00640000')
        self.assertEqual(p['observed_max_mark'],1.05)
        self.assertFalse(p['complete'])

    def test_scoreboard_preserves_horizon_provenance(self):
        row={'date':'2026-09-08','symbol':'SPY','direction':'BUY','passes':True,'outcome':{'hit':{'t04':True},'stopped':False,'mfe_r':.4}}
        with patch.object(fl,'_read_all',return_value=[row]):
            s=fl.scoreboard()
        self.assertNotEqual(s['horizon']['policy'],'session_close_v1')
        self.assertIn('coverage',s)

if __name__=='__main__':unittest.main()
