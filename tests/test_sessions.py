import unittest
from datetime import date, datetime, timedelta, timezone

from app import make_accounts, run_daily
from formatting import SAFE_LIMIT, Mover
from race import account_series
from sessions import build_session_post, build_session_report, clock_label, live_label, race_post, review_post
from test_race import history
from test_scanner import CALENDAR, FakeClient, asset, bar

OCT2, OCT5, OCT6, OCT7, OCT8, OCT10 = (date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6),
                                       date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 10))
DAYS = [OCT2, OCT5, OCT6, OCT7, OCT8]
OPEN_ET = datetime(2026, 10, 8, 9, 30)
RACE_ET = datetime(2026, 10, 8, 11, 0)
CLOSE_ET = datetime(2026, 10, 8, 16, 0)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class LiveMarket(FakeClient):
    """FakeClient that also answers daily AND intraday portfolio history, like AlpacaClient."""

    def __init__(self, daily=(), intraday=(), **kwargs):
        super().__init__(**kwargs)
        self.daily, self.intraday, self.history_calls = list(daily), list(intraday), []

    def get_portfolio_history(self, period="1A", timeframe="1D"):
        self.request_count += 1
        self.history_calls.append((period, timeframe))
        if timeframe == "1D":
            return history(self.daily)
        return {"timestamp": [int(moment.timestamp()) for moment, _ in self.intraday],
                "equity": [value for _, value in self.intraday]}


def make_market(with_bars=True):
    assets, bars = [], {}
    for i in range(15):
        assets += [asset(f"UP{i:02d}"), asset(f"DN{i:02d}")]
        if with_bars:
            bars[f"UP{i:02d}"] = [bar(d, 10 + n * (i + 1) * 0.1, 1_000_000) for n, d in enumerate(DAYS)]
            bars[f"DN{i:02d}"] = [bar(d, 50 - n * (i + 1) * 0.2, 1_000_000) for n, d in enumerate(DAYS)]
    bars["SPY"] = [bar(d, 670 + n, 60_000_000) for n, d in enumerate(DAYS)]
    bars["QQQ"] = [bar(d, 600 - n, 40_000_000) for n, d in enumerate(DAYS)]
    return LiveMarket(daily=[(OCT2, 100_000), (OCT7, 101_000)],
                      intraday=[(utc(2026, 10, 8, 15, 0), 101_500), (utc(2026, 10, 8, 15, 5), 101_600),
                                (utc(2026, 10, 8, 19, 0), None)],
                      assets=assets, bars=bars)


class TestLabels(unittest.TestCase):
    def test_clock_and_live_labels(self):
        self.assertEqual(clock_label(OPEN_ET), "9:30 AM")
        self.assertEqual(clock_label(datetime(2026, 10, 8, 14, 0)), "2:00 PM")
        self.assertEqual(live_label(RACE_ET), "Thu Oct 8, 11:00 AM ET")


class TestLiveRace(unittest.TestCase):
    def test_account_series_lays_today_on_top_of_daily_closes(self):
        market = make_market()
        self.assertEqual(account_series(market)[-1], (OCT7, 101_000.0))
        self.assertEqual(account_series(market, intraday=True)[-1], (OCT8, 101_600.0))
        self.assertEqual(market.history_calls[-1], ("1D", "5Min"))

    def test_race_post(self):
        market = make_market()
        messages, summary = race_post(market, {"NantBot": market}, CALENDAR, RACE_ET)
        self.assertEqual(len(messages), 1)
        self.assertIn("as of Thu Oct 8, 11:00 AM ET", messages[0])
        self.assertIn("about 15 minutes ago", messages[0])
        self.assertIn("NantBot +1.60%", summary)                   # 100,000 -> 101,600 live
        self.assertEqual(market.bar_calls, [(["SPY"], OCT2)])        # no market scan for race posts


class TestReviews(unittest.TestCase):
    def test_open_reviews_the_last_full_session(self):
        market = make_market()
        messages, summary = review_post("open", market, {"NantBot": market}, CALENDAR, OPEN_ET)
        text = "\n".join(messages)
        self.assertIn("Market Open Review** · recap of Wed Oct 7, 2026", text)
        self.assertIn("Race Scoreboard", text)
        self.assertIn("last full session", text)
        self.assertIn("window 2026-10-06 -> 2026-10-07", summary)
        for message in messages:
            self.assertLessEqual(len(message), SAFE_LIMIT)

    def test_close_reviews_today_with_delay_note(self):
        market = make_market()
        messages, summary = review_post("close", market, {"NantBot": market}, CALENDAR, CLOSE_ET)
        text = "\n".join(messages)
        self.assertIn("Market Close Review** · Thu Oct 8, 2026", text)
        self.assertIn("as of about 3:45 PM ET", text)
        self.assertIn("UP14", text)
        self.assertIn("window 2026-10-07 -> 2026-10-08", summary)

    def test_close_for_a_past_day_says_final(self):
        market = make_market()
        messages, _ = review_post("close", market, {"NantBot": market}, CALENDAR, CLOSE_ET, session_day=OCT7)
        self.assertIn("Final closing prices", "\n".join(messages))

    def test_no_market_data_fails_loudly(self):
        market = make_market(with_bars=False)
        with self.assertRaises(RuntimeError):
            review_post("close", market, {"NantBot": market}, CALENDAR, CLOSE_ET)

    def test_weekend_session_is_skipped(self):
        messages, reason = review_post("close", make_market(), {}, CALENDAR, CLOSE_ET, session_day=OCT10)
        self.assertIsNone(messages)
        self.assertIn("not a trading day", reason)

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            build_session_post("lunch", make_market(), {}, CALENDAR, RACE_ET)


MOOD = "Market mood: 10 of 14 funds above their 200-day average (bullish)"


def make_market_with_fund_history():
    """make_market() plus 220 days of prices for the 14 funds, so the mood can be judged."""
    from test_mood import fund_bars
    from test_scanner import weekdays
    market = make_market()
    market.bars.update(fund_bars(days=weekdays(OCT8 - timedelta(days=330), OCT8)[-220:]))
    return market


class TestMarketMoodInThePost(unittest.TestCase):

    def test_task_b_the_mood_goes_under_the_market_lines(self):
        """TASK B: this passes once build_session_report adds the mood line."""
        indexes = [Mover("SPY", 672.41, 0.84, 61_200_000), Mover("QQQ", 598.10, -0.37, 38_900_000)]
        text = "\n".join(build_session_report("HEADER", indexes, [], [], "BOARD", "FOOTER", mood=MOOD))
        self.assertIn("(-0.37%)\n" + MOOD + "\n", text)                # on its own line, right after QQQ
        self.assertLess(text.index(MOOD), text.index("Top Gainers"))    # inside the Market section

    def test_no_mood_leaves_the_post_exactly_as_it_was(self):
        indexes = [Mover("SPY", 672.41, 0.84, 61_200_000)]
        without = build_session_report("HEADER", indexes, [], [], "BOARD", "FOOTER")
        empty = build_session_report("HEADER", indexes, [], [], "BOARD", "FOOTER", mood="")
        self.assertEqual(without, empty)
        self.assertNotIn("Market mood", "\n".join(without))
        self.assertIn("**Market**\n🟢 **SPY** $672.41 (+0.84%)\n\n**🚀 Top Gainers**", "\n".join(without))

    def test_the_open_review_shows_the_mood(self):
        """Needs TASK A and TASK B: the whole path, from Alpaca's bars to the post."""
        market = make_market_with_fund_history()
        messages, summary = review_post("open", market, {"NantBot": market}, CALENDAR, OPEN_ET)
        self.assertIn(MOOD, "\n".join(messages))
        self.assertIn(MOOD, summary)
        for message in messages:
            self.assertLessEqual(len(message), SAFE_LIMIT)

    def test_the_close_review_shows_the_mood_too(self):
        market = make_market_with_fund_history()
        messages, _ = review_post("close", market, {"NantBot": market}, CALENDAR, CLOSE_ET)
        self.assertIn(MOOD, "\n".join(messages))

    def test_race_posts_do_not_fetch_the_mood(self):
        market = make_market_with_fund_history()
        messages, _ = race_post(market, {"NantBot": market}, CALENDAR, RACE_ET)
        self.assertNotIn("Market mood", "\n".join(messages))
        self.assertEqual(market.bar_calls, [(["SPY"], OCT2)])

    def test_a_mood_problem_never_stops_the_review(self):
        market = make_market_with_fund_history()
        market.fail_symbols = {"TLT"}                       # the mood request fails; the scan does not ask for TLT
        messages, summary = review_post("open", market, {"NantBot": market}, CALENDAR, OPEN_ET)
        text = "\n".join(messages)
        self.assertIn("Market Open Review", text)
        self.assertIn("Race Scoreboard", text)
        self.assertNotIn("Market mood", text)
        self.assertNotIn("Market mood", summary)

    def test_without_fund_history_the_post_is_unchanged(self):
        market = make_market()                               # SPY and QQQ only have 5 days here
        messages, _ = review_post("open", market, {"NantBot": market}, CALENDAR, OPEN_ET)
        self.assertNotIn("Market mood", "\n".join(messages))


SECRETS = {"key_id": "NKEY", "secret_key": "NS", "webhook_url": "https://discord.example/hook",
           "sparticus_key_id": "SKEY", "sparticus_secret_key": "SS"}


class TestScheduledWeekdayRuns(unittest.TestCase):
    def setUp(self):
        self.market = make_market()
        self.sparticus = LiveMarket(daily=[(OCT2, 50_000), (OCT7, 49_000)],
                                    intraday=[(utc(2026, 10, 8, 15, 0), 49_500)])
        self.posted, self.logs = [], []

    def make_client(self, key_id, secret_key):
        return self.sparticus if key_id == "SKEY" else self.market

    def poster(self, url, messages):
        self.posted.append(messages)
        return [204] * len(messages)

    def run_job(self, event, now_utc, secrets=SECRETS):
        return run_daily(event, now_utc, secrets, make_client=self.make_client,
                         poster=self.poster, log=self.logs.append)

    def test_930_open_review_posts(self):
        outcome = self.run_job({"source": "schedule", "mode": "open"}, utc(2026, 10, 8, 13, 30))
        self.assertEqual((outcome["status"], outcome["mode"]), ("posted", "open"))
        text = "\n".join(self.posted[0])
        self.assertIn("Market Open Review", text)
        self.assertIn("Sparticus", text)
        self.assertNotIn("Sparticus: no data yet", text)

    def test_race_updates_post_only_the_board(self):
        outcome = self.run_job({"source": "schedule", "mode": "race"}, utc(2026, 10, 8, 16, 0))   # 12:00 PM ET
        self.assertEqual(outcome["status"], "posted")
        self.assertIn("as of Thu Oct 8, 12:00 PM ET", self.posted[0][0])
        self.assertNotIn("Top Gainers", self.posted[0][0])

    def test_close_dry_run_does_not_post(self):
        outcome = self.run_job({"source": "schedule", "mode": "close", "dry_run": True}, utc(2026, 10, 8, 20, 0))
        self.assertEqual(outcome["status"], "dry_run")
        self.assertIn("Market Close Review", outcome["messages"][0])
        self.assertEqual(self.posted, [])

    def test_holiday_skips_every_mode_quietly(self):
        self.market.calendar = [d for d in CALENDAR if d != OCT8]
        for mode in ("open", "race", "close"):
            outcome = self.run_job({"source": "schedule", "mode": mode}, utc(2026, 10, 8, 15, 0))
            self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(self.posted, [])
        self.assertEqual(self.market.bar_calls, [])

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            self.run_job({"mode": "lunch"}, utc(2026, 10, 8, 15, 0))

    def test_no_mode_keeps_the_old_daily_behavior(self):
        outcome = self.run_job({"date": "2026-10-07", "dry_run": True}, utc(2026, 10, 8, 15, 0))
        self.assertEqual(outcome["status"], "dry_run")
        self.assertIn("Daily Breakdown", outcome["preview"])

    def test_make_accounts_without_sparticus(self):
        logs = []
        nantbot, accounts = make_accounts({"key_id": "N", "secret_key": "S"}, lambda k, s: k, logs.append)
        self.assertEqual((nantbot, accounts), ("N", {"NantBot": "N", "Sparticus": None}))
        self.assertTrue(any("no Sparticus keys" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
