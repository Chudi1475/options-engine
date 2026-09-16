"""Issue 7: the bot texts trades, not news.

Owner instruction 2026-09-16, after one day put 60 to 70 catalyst watch and
breaking-news messages on his phone against barely any trades. Every push that
is news rather than a trade is off unless NEWS_ALERTS_ENABLED says otherwise.

Each test here fails on the build that spammed him and passes after the change.
The last one pins the half of the news layer that STAYS: news still makes the
bot skip a trade it should not take. Only the texting stopped.
"""
import os
import tempfile

os.environ['BOT_TEST_MODE'] = '1'
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='kelbot_quiet_')

import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import config
import news
import scanner

ET = ZoneInfo('America/New_York')


def _svc():
    svc = scanner.Service.__new__(scanner.Service)
    svc.dry = False
    return svc


class _Dummy:
    """Stands in for quotes.ChainProcess so a fail-before run of these tests
    never spawns the real provider subprocess."""

    def __init__(self, *a, **k):
        pass

    def read(self, *a, **k):
        return {'events': [], 'errors': []}

    def close(self):
        pass


class NewsPushesAreOff(unittest.TestCase):

    def setUp(self):
        for key in ('NEWS_ALERTS_ENABLED', 'CATALYST_WATCH_ENABLED'):
            os.environ.pop(key, None)

    tearDown = setUp

    def test_quiet_is_the_default(self):
        self.assertFalse(config.news_alerts_enabled())

    def test_the_owner_can_put_them_back(self):
        for value in ('1', 'true', 'TRUE', 'yes', 'on'):
            os.environ['NEWS_ALERTS_ENABLED'] = value
            self.assertTrue(config.news_alerts_enabled(), value)
        for value in ('0', 'false', 'no', 'off', ''):
            os.environ['NEWS_ALERTS_ENABLED'] = value
            self.assertFalse(config.news_alerts_enabled(), value)

    def test_the_breaking_news_thread_never_starts(self):
        # standby is forced OFF so the only thing that can stop the thread is
        # the new rule. On the old build this spawns the watcher and fails.
        svc = _svc()
        with patch.object(scanner.telegram, 'standby', return_value=(False, '')), \
                patch.object(scanner.Service, '_news_worker', lambda self: None):
            svc.start_news_watch()
        self.assertIsNone(getattr(svc, '_news_thread', None))

    def test_the_breaking_news_thread_still_starts_when_asked_for(self):
        os.environ['NEWS_ALERTS_ENABLED'] = 'true'
        svc = _svc()
        with patch.object(scanner.telegram, 'standby', return_value=(False, '')), \
                patch.object(scanner.Service, '_news_worker', lambda self: None):
            svc.start_news_watch()
            thread = getattr(svc, '_news_thread', None)
            self.assertIsNotNone(thread)
            svc.stop_news_watch()
            thread.join(timeout=5)

    def test_the_catalyst_watch_collects_nothing(self):
        # the loop that sent the AVGO and MU notices in the owner's screenshots
        svc = _svc()
        with patch.object(scanner.telegram, 'test_mode', return_value=False), \
                patch.object(scanner.telegram, 'may_write_shared_state',
                             return_value=True), \
                patch.object(scanner.quotes, 'ChainProcess', _Dummy):
            svc.maybe_catalyst_watch(datetime(2026, 9, 16, 10, 0, tzinfo=ET))
        self.assertIsNone(getattr(svc, '_catalyst_worker', None))

    def test_the_morning_card_carries_no_headline_lines(self):
        svc = _svc()
        svc.cfg = type('Cfg', (), {'watchlist': {'QCOM': 1}})()
        with patch.object(scanner.news, 'morning_lines',
                          return_value=['\U0001F4F0 CNBC: tariff war']) as lines:
            self.assertEqual(svc._morning_news_lines(), [])
        lines.assert_not_called()

    def test_the_morning_card_keeps_them_when_asked_for(self):
        os.environ['NEWS_ALERTS_ENABLED'] = 'true'
        svc = _svc()
        svc.cfg = type('Cfg', (), {'watchlist': {'QCOM': 1}})()
        with patch.object(scanner.news, 'morning_lines',
                          return_value=['\U0001F4F0 CNBC: tariff war']):
            self.assertEqual(len(svc._morning_news_lines()), 1)

    def test_a_broken_feed_still_cannot_break_the_morning_card(self):
        os.environ['NEWS_ALERTS_ENABLED'] = 'true'
        svc = _svc()
        svc.cfg = type('Cfg', (), {'watchlist': {'QCOM': 1}})()
        with patch.object(scanner.news, 'morning_lines',
                          side_effect=RuntimeError('feed down')):
            self.assertEqual(svc._morning_news_lines(), [])

    def test_an_entry_card_carries_no_news_line(self):
        svc = _svc()
        with patch.object(scanner.news, 'hot_headlines',
                          return_value=[('CNBC', 'tariff war')]) as headlines:
            self.assertEqual(svc._entry_news_lines('QCOM'), [])
        headlines.assert_not_called()

    def test_an_entry_card_keeps_it_when_asked_for(self):
        os.environ['NEWS_ALERTS_ENABLED'] = 'true'
        svc = _svc()
        with patch.object(scanner.news, 'hot_headlines',
                          return_value=[('CNBC', 'tariff war')]):
            self.assertEqual(len(svc._entry_news_lines('QCOM')), 1)
            self.assertEqual(svc._entry_news_lines('SPX'), [])

    def test_news_still_makes_the_bot_skip_a_trade(self):
        """The picking half of the news layer is untouched on purpose: an
        option whose life contains an earnings report is a coin flip on the
        report, and that skip is what he means by not failing."""
        self.assertFalse(config.news_alerts_enabled())
        with patch.object(news, 'next_earnings',
                          return_value=date(2026, 9, 17)), \
                patch.object(news, '_today', return_value=date(2026, 9, 16)):
            blocked, when = news.earnings_inside('QCOM', date(2026, 9, 18))
        self.assertTrue(blocked)
        self.assertEqual(when, date(2026, 9, 17))


if __name__ == '__main__':
    unittest.main()
