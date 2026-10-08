import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import app
from app import run_weekly, secrets_from_ssm
from formatting import SAFE_LIMIT, Mover
from market_math import Window
from test_app import FakeSSM
from test_race import FakeAccount, history
from test_scanner import FakeClient, asset, bar
from weekly import build_weekly, build_weekly_report, week_label

OCT2, OCT5, OCT8, OCT9 = date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 8), date(2026, 10, 9)
WEEK = [OCT5 + timedelta(days=i) for i in range(5)]   # Mon Oct 5 .. Fri Oct 9


def week_bars(start_close, end_close, volume=1_000_000):
    """A bar for Fri Oct 2 (the base day) plus Mon-Fri, sliding from start to end."""
    days = [OCT2] + WEEK
    step = (end_close - start_close) / (len(days) - 1)
    return [bar(d, round(start_close + i * step, 4), volume) for i, d in enumerate(days)]


class MarketWithHistory(FakeClient):
    """FakeClient that also answers get_portfolio_history (like a real AlpacaClient)."""

    def __init__(self, equity_points=(), **kwargs):
        super().__init__(**kwargs)
        self.equity_points = list(equity_points)

    def get_portfolio_history(self):
        self.request_count += 1
        return history(self.equity_points)


def make_market():
    assets, bars = [], {}
    for i in range(15):
        assets.append(asset(f"UP{i:02d}"))
        bars[f"UP{i:02d}"] = week_bars(10, 10 + i + 1)        # +10% .. +150%
        assets.append(asset(f"DN{i:02d}"))
        bars[f"DN{i:02d}"] = week_bars(20, 20 - (i + 1) * 0.5)
    bars["SPY"] = week_bars(670, 680, volume=60_000_000)
    bars["QQQ"] = week_bars(600, 590, volume=40_000_000)
    return MarketWithHistory(equity_points=[(OCT2, 100_000), (OCT9, 101_000)], assets=assets, bars=bars)


SATURDAY_9AM_ET = datetime(2026, 10, 10, 9, 0)


class TestReport(unittest.TestCase):
    def test_week_label(self):
        self.assertEqual(week_label(Window(OCT2, tuple(WEEK))), "Mon Oct 5 – Fri Oct 9, 2026")

    def test_report_fits_discord(self):
        movers = [Mover(f"S{i:03d}", 123.45, 12.3 - i, 4_500_000) for i in range(10)]
        messages = build_weekly_report("Mon Oct 5 – Fri Oct 9, 2026", movers[:2], movers, movers,
                                       "**🏁 Race Scoreboard**\n(no data yet)")
        self.assertIn("Weekly Breakdown", messages[0])
        self.assertIn("Race Scoreboard", messages[-1])
        for message in messages:
            self.assertLessEqual(len(message), SAFE_LIMIT)


class TestBuildWeekly(unittest.TestCase):
    def test_saturday_run_covers_monday_to_friday(self):
        market = make_market()
        sparticus = FakeAccount([(OCT2, 100_000), (OCT9, 98_000)])
        result, messages, racers = build_weekly(market, {"NantBot": market, "Sparticus": sparticus},
                                                SATURDAY_9AM_ET)
        self.assertEqual(result.window, Window(OCT2, tuple(WEEK)))
        self.assertEqual(result.gainers[0].symbol, "UP14")
        self.assertAlmostEqual(result.gainers[0].change_pct, 150.0)
        self.assertEqual(result.losers[0].symbol, "DN14")
        text = "\n".join(messages)
        self.assertIn("Mon Oct 5 – Fri Oct 9, 2026", text)
        self.assertIn("🥇 SPY", text)              # SPY +1.49% beats NantBot +1.00%
        self.assertIn("Sparticus is trailing SPY", text)
        self.assertEqual(sorted(r.name for r in racers), ["NantBot", "SPY", "Sparticus"])
        first_calendar_day = market.calendar[0]
        self.assertLessEqual(first_calendar_day, date(2026, 9, 14))

    def test_explicit_date_picks_that_week(self):
        market = make_market()
        result, _, _ = build_weekly(market, {"NantBot": market}, SATURDAY_9AM_ET, as_of=OCT8)
        self.assertEqual(result.window.end_day, OCT8)          # week-to-date through Thursday


SECRETS = {"key_id": "NKEY", "secret_key": "NSECRET", "webhook_url": "https://discord.example/hook"}


class TestRunWeekly(unittest.TestCase):
    def setUp(self):
        self.market = make_market()
        self.sparticus = MarketWithHistory(equity_points=[(OCT2, 50_000), (OCT9, 51_000)])
        self.made, self.posted, self.logs = [], [], []

    def make_client(self, key_id, secret_key):
        self.made.append(key_id)
        return self.sparticus if key_id == "SKEY" else self.market

    def poster(self, url, messages):
        self.posted.append((url, messages))
        return [204] * len(messages)

    def run_job(self, event, secrets):
        return run_weekly(event, datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc), secrets,
                          make_client=self.make_client, poster=self.poster, log=self.logs.append)

    def test_scheduled_saturday_posts_with_sparticus(self):
        secrets = dict(SECRETS, sparticus_key_id="SKEY", sparticus_secret_key="SSECRET")
        outcome = self.run_job({"source": "schedule"}, secrets)
        self.assertEqual(outcome["status"], "posted")
        self.assertEqual(outcome["week"], "2026-10-05..2026-10-09")
        self.assertEqual(self.made, ["NKEY", "SKEY"])
        text = "\n".join(self.posted[0][1])
        self.assertIn("Sparticus", text)
        self.assertNotIn("Sparticus: no data yet", text)
        self.assertTrue(any(line.startswith("race: Sparticus") for line in self.logs))

    def test_without_sparticus_keys(self):
        outcome = self.run_job({"source": "schedule"}, SECRETS)
        self.assertEqual(outcome["status"], "posted")
        self.assertEqual(self.made, ["NKEY"])
        self.assertIn("Sparticus: no data yet", "\n".join(self.posted[0][1]))

    def test_shared_account_warns(self):
        secrets = dict(SECRETS, sparticus_key_id="NKEY", sparticus_secret_key="NSECRET")
        self.run_job({"dry_run": True}, secrets)
        self.assertTrue(any("share one Alpaca account" in line for line in self.logs))

    def test_dry_run_returns_messages_and_never_posts(self):
        outcome = self.run_job({"dry_run": True}, SECRETS)
        self.assertEqual(outcome["status"], "dry_run")
        self.assertIn("Weekly Breakdown", outcome["messages"][0])
        self.assertEqual(self.posted, [])


class TestSecrets(unittest.TestCase):
    ENV = {"ALPACA_KEY_ID_PARAM": "/k", "ALPACA_SECRET_KEY_PARAM": "/s", "DISCORD_WEBHOOK_PARAM": "/w",
           "SPARTICUS_KEY_ID_PARAM": "/sk", "SPARTICUS_SECRET_KEY_PARAM": "/ss"}

    def test_optional_secrets_present(self):
        ssm = FakeSSM({"/k": "K", "/s": "S", "/w": "W", "/sk": "SK", "/ss": "SS"})
        self.assertEqual(secrets_from_ssm(ssm, self.ENV),
                         {"key_id": "K", "secret_key": "S", "webhook_url": "W",
                          "sparticus_key_id": "SK", "sparticus_secret_key": "SS"})

    def test_optional_secrets_missing_is_fine(self):
        ssm = FakeSSM({"/k": "K", "/s": "S", "/w": "W"}, invalid=["/sk", "/ss"])
        self.assertEqual(secrets_from_ssm(ssm, self.ENV), {"key_id": "K", "secret_key": "S", "webhook_url": "W"})

    def test_required_secret_missing_fails(self):
        ssm = FakeSSM({"/k": "K", "/s": "S"}, invalid=["/w"])
        with self.assertRaises(RuntimeError):
            secrets_from_ssm(ssm, self.ENV)

    def test_daily_function_without_sparticus_env_asks_for_three(self):
        env = {k: v for k, v in self.ENV.items() if not k.startswith("SPARTICUS")}
        ssm = FakeSSM({"/k": "K", "/s": "S", "/w": "W"})
        secrets_from_ssm(ssm, env)
        self.assertEqual(ssm.calls[0][0], ["/k", "/s", "/w"])

    def test_weekly_handler_wiring(self):
        fake_boto3 = mock.Mock()
        fake_boto3.client.return_value = FakeSSM({"/k": "K", "/s": "S", "/w": "W", "/sk": "SK", "/ss": "SS"})
        with mock.patch.dict(os.environ, self.ENV), mock.patch.dict(sys.modules, {"boto3": fake_boto3}), \
                mock.patch.object(app, "run_weekly", return_value={"status": "ok"}) as fake_run:
            self.assertEqual(app.weekly_handler(None, None), {"status": "ok"})
        event, _, secrets = fake_run.call_args[0]
        self.assertEqual(event, {})
        self.assertEqual(secrets["sparticus_key_id"], "SK")


if __name__ == "__main__":
    unittest.main()
