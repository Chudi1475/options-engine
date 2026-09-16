"""The running per-setup record that goes on every alert.

Owner instruction 2026-09-16: every alert says how many times that exact trade
has been taken and how many of those hit, counted from the first one, and the
denominator grows as trades close so the next day's card already includes
today. He, Ryan and Kelechi read that number to decide for themselves.

These fail on the build that had no such tally. The important one is
test_it_reports_from_the_very_first_trade: the card's quoted expectancy still
waits for the config thresholds before switching to live numbers, and this
tally deliberately does not.
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()

import tempfile
import unittest
from pathlib import Path

import config

config.DATA_DIR = Path(tempfile.mkdtemp(prefix="kelbot_record_"))

import cards       # noqa: E402
import scoreboard  # noqa: E402


class FakePos:
    """Only what setup_record touches: the key, the result, the date."""

    def __init__(self, key, pnl, day="2026-06-15"):
        self._key, self.final_pnl_pct, self.date = key, pnl, day

    def setup_key(self):
        return self._key


class FakeBook:
    def __init__(self, rows):
        self._rows = list(rows)

    def closed(self):
        return self._rows


class TheRunningRecord(unittest.TestCase):

    def test_nothing_closed_yet_is_not_a_rate_of_zero(self):
        self.assertIsNone(scoreboard.setup_record(FakeBook([]), "TSLA", "put"))

    def test_it_reports_from_the_very_first_trade(self):
        book = FakeBook([FakePos("TSLA:put", 30.0)])
        record = scoreboard.setup_record(book, "TSLA", "put")
        self.assertEqual(record["trades"], 1)
        self.assertEqual(record["wins"], 1)

    def test_it_counts_wins_over_takes(self):
        book = FakeBook([FakePos("TSLA:put", 30.0), FakePos("TSLA:put", -50.0),
                         FakePos("TSLA:put", 12.0), FakePos("TSLA:put", 40.0)])
        record = scoreboard.setup_record(book, "TSLA", "put")
        self.assertEqual((record["wins"], record["trades"]), (3, 4))
        self.assertEqual(record["win_rate"], 75.0)

    def test_a_breakeven_trade_is_not_a_win(self):
        book = FakeBook([FakePos("SPY:call", 0.0), FakePos("SPY:call", 10.0)])
        self.assertEqual(scoreboard.setup_record(book, "SPY", "call")["wins"], 1)

    def test_one_setup_cannot_borrow_another_setups_record(self):
        book = FakeBook([FakePos("TSLA:put", 30.0), FakePos("SPY:call", -50.0)])
        self.assertEqual(scoreboard.setup_record(book, "TSLA", "put")["wins"], 1)
        self.assertEqual(scoreboard.setup_record(book, "SPY", "call")["wins"], 0)
        self.assertEqual(scoreboard.setup_record(book, "SPY", "call")["trades"], 1)

    def test_an_ungraded_trade_is_left_out_not_called_a_loss(self):
        book = FakeBook([FakePos("SPX:call", 30.0), FakePos("SPX:call", None)])
        record = scoreboard.setup_record(book, "SPX", "call")
        self.assertEqual((record["wins"], record["trades"]), (1, 1))
        self.assertEqual(record["ungraded"], 1)

    def test_the_denominator_grows_as_trades_close(self):
        rows = [FakePos("TSLA:put", 30.0)]
        first = scoreboard.setup_record(FakeBook(rows), "TSLA", "put")
        rows.append(FakePos("TSLA:put", -50.0))
        second = scoreboard.setup_record(FakeBook(rows), "TSLA", "put")
        self.assertEqual(first["trades"], 1)
        self.assertEqual(second["trades"], 2)
        self.assertEqual(second["wins"], 1)

    def test_every_setup_is_listed_for_the_daily_retally(self):
        book = FakeBook([FakePos("TSLA:put", 30.0), FakePos("SPY:call", -50.0)])
        every = scoreboard.all_setup_records(book)
        self.assertEqual(set(every), {"TSLA:put", "SPY:call"})


class TheLineOnTheCard(unittest.TestCase):

    def test_it_states_the_hits_over_the_takes(self):
        book = FakeBook([FakePos("TSLA:put", 30.0), FakePos("TSLA:put", -50.0),
                         FakePos("TSLA:put", 40.0)])
        line = cards.record_line(scoreboard.setup_record(book, "TSLA", "put"),
                                 "TSLA", "put")
        self.assertIn("2 of 3", line)
        self.assertIn("TSLA PUT", line)

    def test_an_empty_record_says_so_instead_of_quoting_a_rate(self):
        line = cards.record_line(None, "TSLA", "put")
        self.assertIn("first one", line)
        self.assertNotIn("of 100", line)

    def test_the_ungraded_ones_are_named_not_hidden(self):
        book = FakeBook([FakePos("SPX:call", 30.0), FakePos("SPX:call", None)])
        line = cards.record_line(scoreboard.setup_record(book, "SPX", "call"),
                                 "SPX", "call")
        self.assertIn("1 of 1", line)
        self.assertIn("without a price", line)


class TheRecordNeverStopsTheAlert(unittest.TestCase):
    """A tally is cosmetic; the trade alert is not. This pins that order.

    Caught for real: the first wiring called the tally straight from
    open_position, so a book that could not answer raised THROUGH the entry
    path and the card never went out."""

    def _service(self, book):
        import scanner
        svc = scanner.Service.__new__(scanner.Service)
        svc.book = book
        return svc

    def test_a_book_that_cannot_answer_costs_the_line_not_the_trade(self):
        self.assertIsNone(self._service(object())._setup_record("TSLA", "put"))

    def test_a_book_that_raises_costs_the_line_not_the_trade(self):
        class Angry:
            def closed(self):
                raise RuntimeError("volume gone")
        self.assertIsNone(self._service(Angry())._setup_record("TSLA", "put"))

    def test_a_working_book_still_returns_the_record(self):
        svc = self._service(FakeBook([FakePos("TSLA:put", 30.0)]))
        self.assertEqual(svc._setup_record("TSLA", "put")["trades"], 1)


if __name__ == "__main__":
    unittest.main()
