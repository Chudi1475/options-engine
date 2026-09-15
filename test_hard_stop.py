"""Regression for the hard stop (issue 3).

The stop is -50 by default, set with the STOP_PCT env var, and stamped on every
position when it opens. A trade that is already open keeps the stop it opened
under, so a deploy that moves the setting can never sell it early. Live and
paper positions run through the same step(), and the half, the give-back trail
and the old-rules shadow are untouched. Every surface that talks about one
trade (the stop card, the log line, the recap, the recorder, the weekly board)
names that trade's own stop.

    python test_hard_stop.py
"""
import os
import tempfile

os.environ["BOT_TEST_MODE"] = "1"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelbot_hard_stop_")

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import unittest  # noqa: E402
from dataclasses import asdict  # noqa: E402
from datetime import date, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import cards  # noqa: E402
import config  # noqa: E402
import positions  # noqa: E402
import recap  # noqa: E402
import scanner  # noqa: E402
import scoreboard  # noqa: E402

REPO = Path(__file__).parent
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 14, 11, 0, tzinfo=ET)
ENTRY = 2.00

# A child interpreter that cannot see any .env, so the built-in default is what
# it reports no matter what a developer's .env says.
CHILD = (
    "import pathlib\n"
    "_exists = pathlib.Path.exists\n"
    "pathlib.Path.exists = lambda self, *a, **k: "
    "False if self.name == '.env' else _exists(self, *a, **k)\n"
    "import config\n"
    "print(config.STOP_PCT)\n"
)


def mark_at(pct):
    return ENTRY * (1 + pct / 100)


def new_position(pid="hs", paper=False):
    return positions.Position(
        id=pid, date="2026-09-14", time_et="10:00:00", ticker="SPY",
        direction="call", right="C", strike=640.0, expiry="2026-09-18",
        entry_mid=ENTRY, entry_source="quote", paper=paper)


def step(pos, pct, comparable=True, est_pct=None):
    return positions.step(pos, NOW, mark_at(pct), "quote", est_pct, False,
                          positions.DEFAULT_OLD_BRACKET, comparable)


def types(events):
    return [e["type"] for e in events]


def legacy_row(pos):
    """A saved row as it looked before stop_pct existed."""
    row = json.loads(json.dumps(asdict(pos)))
    del row["stop_pct"]
    return row


def stop_in_child(extra_env):
    env = {k: v for k, v in os.environ.items() if k != "STOP_PCT"}
    env.update(extra_env)
    r = subprocess.run([sys.executable, "-c", CHILD], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise AssertionError(f"config import failed: {r.stderr[-400:]}")
    return float(r.stdout.strip().splitlines()[-1])


class HardStopTests(unittest.TestCase):
    def setUp(self):
        self._saved = config.STOP_PCT
        config.STOP_PCT = config.DEFAULT_STOP_PCT

    def tearDown(self):
        config.STOP_PCT = self._saved

    # --- the setting ------------------------------------------------------

    def test_default_is_minus_fifty(self):
        self.assertEqual(config.DEFAULT_STOP_PCT, -50.0)
        self.assertEqual(stop_in_child({}), config.DEFAULT_STOP_PCT)

    def test_env_var_overrides_the_default(self):
        self.assertEqual(stop_in_child({"STOP_PCT": "-35"}), -35.0)

    def test_sizing_still_risks_one_full_stop_out(self):
        risk = config.RISK_PER_TRADE_PCT
        alloc = config.suggested_alloc_pct(risk)
        self.assertAlmostEqual(alloc * abs(config.STOP_PCT) / 100.0, risk, places=9)
        self.assertAlmostEqual(config.suggested_alloc_pct(1.0), 2.0, places=9)

    # --- the exit ---------------------------------------------------------

    def test_new_position_stamps_the_live_stop(self):
        self.assertEqual(new_position().stop_pct, -50.0)
        config.STOP_PCT = -42.0
        self.assertEqual(new_position().stop_pct, -42.0)

    def test_exits_at_minus_fifty_live_and_paper(self):
        for paper in (False, True):
            with self.subTest(paper=paper):
                p = new_position(paper=paper)
                self.assertEqual(step(p, -49.9), [])
                self.assertEqual(p.state, "open")
                ev = step(p, -50.0)
                self.assertEqual(types(ev), ["stop"])
                self.assertEqual(ev[0]["stop_pct"], -50.0)
                self.assertEqual(p.state, "closed")
                self.assertEqual(p.final_exit["reason"], "stop")
                self.assertAlmostEqual(p.final_pnl_pct, -50.0, places=2)

    def test_open_trade_keeps_the_stop_it_opened_under(self):
        config.STOP_PCT = -90.0
        opened_before = new_position("before")
        config.STOP_PCT = -50.0
        opened_after = new_position("after")
        self.assertEqual(step(opened_before, -60.0), [])
        self.assertEqual(opened_before.state, "open")
        self.assertEqual(types(step(opened_after, -60.0)), ["stop"])
        ev = step(opened_before, -90.0)
        self.assertEqual(types(ev), ["stop"])
        self.assertEqual(ev[0]["stop_pct"], -90.0)

    def test_estimate_only_cycle_uses_the_stamp_not_the_live_setting(self):
        config.STOP_PCT = -90.0
        wide = new_position("est-wide")
        config.STOP_PCT = -50.0
        self.assertEqual(step(wide, -20.0, comparable=False, est_pct=-55.0), [])
        tight = new_position("est-tight")
        config.STOP_PCT = -90.0
        self.assertEqual(types(step(tight, -20.0, comparable=False, est_pct=-55.0)), ["stop"])

    def test_half_and_give_back_trail_are_unchanged(self):
        p = new_position("runner")
        half = config.TP_HALF_PCT + 1
        peak = half + 50
        self.assertEqual(types(step(p, half)), ["sell_half"])
        step(p, peak)
        self.assertEqual(p.state, "half_sold")
        self.assertEqual(types(step(p, peak - config.RUNNER_GIVEBACK_PCT)), ["runner_trail"])

    def test_runner_leg_is_still_covered_by_the_hard_stop(self):
        p = new_position("runner-stop")
        half = config.TP_HALF_PCT + 1
        step(p, half)
        self.assertEqual(types(step(p, -50.0)), ["stop"])
        self.assertAlmostEqual(p.final_pnl_pct, (half - 50.0) / 2, places=2)

    def test_old_rules_shadow_still_uses_its_own_bracket(self):
        config.STOP_PCT = -90.0
        p = new_position("shadow")
        self.assertEqual(step(p, -65.0), [])
        self.assertEqual(p.state, "open")
        self.assertEqual(p.old_rules["exit_reason"], "old stop")

    def test_malformed_stamp_falls_back_to_the_live_setting(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, 0, 5.0, None, "-50",
                    -100.0, -150.0):
            with self.subTest(bad=bad):
                p = new_position("bad")
                p.stop_pct = bad
                self.assertIsNone(positions.stamped_stop(p))
                self.assertEqual(positions.stop_level(p), config.STOP_PCT)

    def test_stamp_at_or_below_minus_100_still_gets_a_stop_that_can_fire(self):
        p = new_position("too-wide")
        p.stop_pct = -150.0
        self.assertEqual(types(step(p, -50.0)), ["stop"])

    def test_env_stop_outside_minus_100_to_0_falls_back_to_the_default(self):
        for raw in ("-150", "-100", "0", "5"):
            with self.subTest(raw=raw):
                self.assertEqual(stop_in_child({"STOP_PCT": raw}), config.DEFAULT_STOP_PCT)

    # --- rows saved before the stamp existed --------------------------------

    def test_open_legacy_row_reloads_at_minus_ninety(self):
        path = Path(os.environ["DATA_DIR"]) / "legacy_positions.json"
        path.write_text(json.dumps([legacy_row(new_position("legacy"))]), encoding="utf-8")
        book = positions.PositionBook(path)
        self.assertEqual(len(book.positions), 1)
        legacy = book.positions[0]
        self.assertEqual(positions.LEGACY_STOP_PCT, -90.0)
        self.assertEqual(legacy.stop_pct, positions.LEGACY_STOP_PCT)
        self.assertEqual(step(legacy, -60.0), [])
        self.assertEqual(legacy.state, "open")

    def test_closed_legacy_row_gets_no_stamp(self):
        p = new_position("closed-legacy")
        step(p, -50.0)
        row = legacy_row(p)
        self.assertEqual(row["state"], "closed")
        back = positions.position_from_row(row)
        self.assertIsNone(back.stop_pct)
        self.assertIsNone(positions.stamped_stop(back))

    def test_saved_stamp_survives_a_reload(self):
        config.STOP_PCT = -70.0
        row = json.loads(json.dumps(asdict(new_position("stamped"))))
        config.STOP_PCT = -50.0
        self.assertEqual(positions.position_from_row(row).stop_pct, -70.0)

    def test_replayed_intent_keeps_the_legacy_stop(self):
        p = new_position("replayed")
        p.decision_id = "dec-replayed"
        svc = scanner.Service.__new__(scanner.Service)
        svc.book = positions.PositionBook(Path(os.environ["DATA_DIR"]) / "replay_positions.json")
        intent = SimpleNamespace(payload={"position": legacy_row(p)},
                                 journal_id="j-replayed", decision_id="dec-replayed")
        pos, landed = svc._reopen_from_intent(intent)
        self.assertTrue(landed)
        self.assertEqual(pos.stop_pct, positions.LEGACY_STOP_PCT)
        self.assertEqual(step(pos, -60.0), [])
        self.assertEqual(pos.state, "open")

    # --- what people and records are told -----------------------------------

    def test_stop_card_names_the_stop_it_fired_at(self):
        config.STOP_PCT = -90.0
        p = new_position("card")
        config.STOP_PCT = -50.0
        ev = step(p, -91.0)
        text = cards.stop_card(p, ev[0])
        self.assertIn("-90% hard stop", text)
        self.assertNotIn("—", text)

    def test_log_line_says_the_hard_stop_fired_and_at_what_level(self):
        config.STOP_PCT = -90.0
        p = new_position("log", paper=True)
        config.STOP_PCT = -50.0
        ev = step(p, -91.0)
        line = scanner.Service.hard_stop_line(NOW, p, ev[0], "quote")
        self.assertIn("HARD STOP fired", line)
        self.assertIn("paper", line)
        self.assertIn("-91.0%", line)
        self.assertIn("-90% stop", line)

    def test_recap_story_names_the_trades_own_stop(self):
        config.STOP_PCT = -90.0
        p = new_position("recap")
        config.STOP_PCT = -50.0
        step(p, -91.0)
        _, story = recap.position_story(p)
        self.assertIn("It hit the -90% stop", story)
        unstamped = positions.position_from_row(legacy_row(p))
        _, story = recap.position_story(unstamped)
        self.assertIn("It hit its hard stop", story)
        self.assertNotIn("-50% stop", story)

    def test_recorder_threshold_is_the_trades_own_stop(self):
        config.STOP_PCT = -90.0
        p = new_position("recorder")
        config.STOP_PCT = -50.0
        ev = step(p, -91.0)
        seen = []
        original = scanner.trade_recorder.record_event
        scanner.trade_recorder.record_event = lambda **kw: seen.append(kw)
        try:
            svc = scanner.Service.__new__(scanner.Service)
            svc.dry = False
            svc._record_exit_events(p, NOW, ev, "quote", "sample-1", [])
        finally:
            scanner.trade_recorder.record_event = original
        self.assertEqual([k["trigger_threshold"] for k in seen], [-90.0])

    def test_weekly_board_quotes_a_stop_only_when_the_week_shares_one(self):
        config.STOP_PCT = -90.0
        wide = new_position("week-wide")
        config.STOP_PCT = -50.0
        tight = new_position("week-tight")
        step(wide, -91.0)
        step(tight, -50.0)
        book = positions.PositionBook.__new__(positions.PositionBook)
        book.positions = [wide, tight]
        mixed = scoreboard.weekly_report(book, None, None, date(2026, 9, 18))
        self.assertIn("each trade's own entry stop", mixed)
        self.assertNotIn("stop -50", mixed)
        book.positions = [tight]
        uniform = scoreboard.weekly_report(book, None, None, date(2026, 9, 18))
        self.assertIn("stop -50", uniform)

    def test_recap_still_open_story_names_the_trades_own_stop(self):
        config.STOP_PCT = -90.0
        p = new_position("recap-open")
        config.STOP_PCT = -50.0
        self.assertEqual(step(p, -60.0), [])
        verdict, story = recap.position_story(p)
        self.assertTrue(verdict.startswith("STILL OPEN"), verdict)
        self.assertIn("Its hard stop is -90%.", story)

    def test_release_manifest_records_the_stop_as_a_number(self):
        import release_manifest
        declared = release_manifest._effective_config()["STOP_PCT"]
        self.assertEqual(float(declared["declared"]), config.DEFAULT_STOP_PCT)
        self.assertTrue(declared["env_overridable"])


if __name__ == "__main__":
    unittest.main()
