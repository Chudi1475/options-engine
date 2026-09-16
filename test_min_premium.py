"""The money floor: no alert on a contract too cheap to be worth taking.

Owner decision 2026-09-16, "aim for decent money and not ant bite dollars". He
chose $1.00. A $1.00 contract costs $100, so banking half at the take profit
returns roughly twelve dollars per contract while the risk is the whole
premium. Under that it is real risk for lunch money.

Scope, pinned here on purpose: this is a PRICE test on the contract a setup
already chose. It removes alerts and can never create one, and it leaves the
entry signal, the allow-list and the win-rate floor exactly where they were.

Every check fails on the build that had no floor.
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config

config.DATA_DIR = Path(tempfile.mkdtemp(prefix="kelbot_premium_"))

import scanner  # noqa: E402


def _svc():
    return scanner.Service.__new__(scanner.Service)


class TheMoneyFloor(unittest.TestCase):

    def test_the_floor_is_a_dollar(self):
        self.assertEqual(config.MIN_PREMIUM, 1.00)

    def test_a_penny_contract_is_refused(self):
        for price in (0.05, 0.30, 0.75, 0.99):
            self.assertTrue(_svc()._premium_below_floor(price), price)

    def test_the_floor_itself_is_allowed(self):
        self.assertFalse(_svc()._premium_below_floor(1.00))

    def test_a_real_contract_is_allowed(self):
        for price in (1.01, 1.89, 3.40, 12.0):
            self.assertFalse(_svc()._premium_below_floor(price), price)

    def test_the_owner_can_move_the_floor(self):
        with patch.object(config, "MIN_PREMIUM", 2.00):
            self.assertTrue(_svc()._premium_below_floor(1.50))
            self.assertFalse(_svc()._premium_below_floor(2.50))

    def test_zero_switches_the_filter_off_entirely(self):
        with patch.object(config, "MIN_PREMIUM", 0):
            self.assertFalse(_svc()._premium_below_floor(0.05))

    def test_a_price_that_will_not_parse_never_blocks_a_trade(self):
        # a cosmetic filter must never be the reason a real trade goes untexted
        for bad in (None, "", "abc", object()):
            self.assertFalse(_svc()._premium_below_floor(bad), repr(bad))

    def test_the_floor_does_not_touch_the_entry_gate(self):
        # the filter is about price only: the win-rate floor and the allow-list
        # are untouched, so this can subtract alerts and never add one
        import live_params
        self.assertEqual(config.MIN_WINRATE, 70.0)
        self.assertEqual(sorted(live_params.DEFAULT_ALLOWED_SETUPS),
                         ["QCOM:call", "SPX:call", "SPY:call", "TSLA:put"])


if __name__ == "__main__":
    unittest.main()
