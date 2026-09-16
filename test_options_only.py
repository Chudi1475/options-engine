"""Issue 8: options only, no forex.

Owner instruction 2026-09-16. The sniper roster carried EUR/USD and USD/JPY.
This is an options bot and he does not trade spot FX, so they are off it.

Every check here fails on the build that still carried them. The last group is
the honesty half: the record a ticket quotes is derived from the published
replay rows restricted to the symbols the gate can still fire, so a card can
never quote a rate that counts trades the bot is no longer allowed to take.
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import.

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config

config.DATA_DIR = Path(tempfile.mkdtemp(prefix="kelbot_options_"))

import fvg            # noqa: E402
import strategy_spec  # noqa: E402

ET = ZoneInfo("America/New_York")
FOREX = ("EURUSD=X", "JPY=X")
REPORT = config.REPO_DIR / "reports" / "chart_backtest_round6_session.json"
# how the report spells the Yahoo codes
NAMES = {"EURUSD=X": "EUR/USD", "JPY=X": "USD/JPY", "^GSPC": "SPX"}


def bars_frame(day: date, times, base=100.0):
    """A tiny OHLC frame at the given ET wall-clock (h, m) tuples."""
    idx = pd.DatetimeIndex([datetime(day.year, day.month, day.day, h, m,
                                     tzinfo=ET) for h, m in times])
    n = len(idx)
    o = [base + i * 0.01 for i in range(n)]
    c = [x + 0.005 for x in o]
    h = [x + 0.02 for x in c]
    lo = [x - 0.02 for x in o]
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c}, index=idx)


DAY = date(2026, 8, 21)
SESSION = bars_frame(DAY, [(9, 30), (9, 35), (9, 40), (9, 45), (9, 50),
                           (9, 55), (10, 0)])
INSIDE_WINDOW = datetime(2026, 8, 21, 10, 5, tzinfo=ET)
ROSTER_REASON = "not a verified sniper symbol"


def _check(symbol):
    return fvg.sniper_check(SESSION, "BUY", 100.05, 0.05, None, symbol,
                            INSIDE_WINDOW)


class ForexIsOffTheRoster(unittest.TestCase):

    def test_no_forex_symbol_is_on_the_roster(self):
        for symbol in FOREX:
            self.assertNotIn(symbol, fvg.SNIPER_SYMBOLS)
        self.assertTrue(fvg.SNIPER_SYMBOLS, "the roster cannot be emptied")

    def test_the_whole_roster_is_now_regular_session(self):
        # nothing left quotes on a US market holiday, so no symbol can fire on
        # a day the round-6 replay never contained
        self.assertEqual(set(fvg.SNIPER_SYMBOLS), set(fvg.SNIPER_RTH_SYMBOLS))

    def test_the_gate_refuses_a_forex_symbol(self):
        for symbol in FOREX:
            out = _check(symbol)
            self.assertFalse(out["passes"], symbol)
            self.assertTrue(any(ROSTER_REASON in r for r in out["reasons"]),
                            f"{symbol}: {out['reasons']}")

    def test_a_roster_symbol_is_refused_for_no_such_reason(self):
        # proves the roster is what rejects forex above, not some other
        # condition this fixture happens to trip
        out = _check("SPY")
        self.assertFalse(any(ROSTER_REASON in r for r in out["reasons"]),
                         out["reasons"])

    def test_the_refusal_reason_is_read_from_the_roster(self):
        reasons = " ".join(_check("EURUSD=X")["reasons"])
        for symbol in sorted(fvg.SNIPER_SYMBOLS):
            self.assertIn(symbol, reasons)
        for symbol in FOREX:
            # the dropped names must not be advertised as acceptable
            self.assertNotIn(f"{symbol} only", reasons)


class TheQuotedRecordFollowsTheRoster(unittest.TestCase):

    def setUp(self):
        self.rep = json.loads(REPORT.read_text(encoding="utf-8"))
        roster = {NAMES.get(s, s) for s in fvg.SNIPER_SYMBOLS}
        self.rows = [r for r in self.rep["trades"]
                     if r["symbol"] in roster
                     and str(r["day"]) >= str(self.rep["split_date"])]
        self.wins = sum(1 for r in self.rows if r["exit"] == "tp")
        self.spec = strategy_spec.get()

    def test_the_record_is_the_roster_rows_and_nothing_else(self):
        self.assertTrue(self.rows)
        self.assertEqual(self.spec.sniper.trades, len(self.rows))
        self.assertEqual(self.spec.sniper.wins, self.wins)
        self.assertEqual(self.spec.sniper.win_rate,
                         round(100.0 * self.wins / len(self.rows), 1))

    def test_the_record_no_longer_counts_the_dropped_trades(self):
        self.assertLess(self.spec.sniper.trades, self.rep["oos"]["trades"])

    def test_the_record_still_names_its_own_report(self):
        self.assertTrue(self.spec.sniper.source.endswith(REPORT.name))

    def test_the_card_claim_pairs_the_rate_with_its_own_count(self):
        claim = self.spec.sniper_card_txt()
        self.assertIn(str(self.spec.sniper.wins), claim)
        self.assertIn(str(self.spec.sniper.trades), claim)

    def test_no_forex_name_reaches_a_text_surface(self):
        surfaces = " ".join([
            " ".join(self.spec.sniper_symbol_names()),
            self.spec.sniper_watch_sentence(),
        ])
        for fragment in ("EUR", "JPY"):
            self.assertNotIn(fragment, surfaces)


if __name__ == "__main__":
    unittest.main()
