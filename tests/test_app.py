import os
import sys
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import app
from app import choose_report_day, eastern_now, eastern_offset_hours, load_secrets, run_daily
from test_scanner import CALENDAR, FakeClient, asset, daily_bars  # reuse the scanner test helpers

OCT5, OCT6, OCT9, OCT10 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 9), date(2026, 10, 10)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class TestEasternTime(unittest.TestCase):
    def test_summer_and_winter_offsets(self):
        self.assertEqual(eastern_offset_hours(utc(2026, 10, 6, 21, 15)), -4)
        self.assertEqual(eastern_offset_hours(utc(2026, 12, 1, 22, 15)), -5)

    def test_scheduled_run_lands_on_same_day(self):
        self.assertEqual(eastern_now(utc(2026, 10, 6, 21, 15)), datetime(2026, 10, 6, 17, 15))
        self.assertEqual(eastern_now(utc(2026, 12, 1, 22, 15)), datetime(2026, 12, 1, 17, 15))

    def test_late_night_utc_is_still_previous_day_in_new_york(self):
        self.assertEqual(eastern_now(utc(2026, 10, 7, 1, 0)).date(), OCT6)

    def test_dst_boundaries_2026(self):
        self.assertEqual(eastern_offset_hours(utc(2026, 3, 8, 6, 59)), -5)   # Mar 8 = 2nd Sunday
        self.assertEqual(eastern_offset_hours(utc(2026, 3, 8, 7, 0)), -4)
        self.assertEqual(eastern_offset_hours(utc(2026, 11, 1, 5, 59)), -4)  # Nov 1 = 1st Sunday
        self.assertEqual(eastern_offset_hours(utc(2026, 11, 1, 6, 0)), -5)


class TestChooseReportDay(unittest.TestCase):
    def test_explicit_date_wins(self):
        self.assertEqual(choose_report_day({"date": "2026-10-05"}, CALENDAR, datetime(2026, 10, 6, 9, 0)), OCT5)

    def test_schedule_reports_today(self):
        self.assertEqual(choose_report_day({"source": "schedule"}, CALENDAR, datetime(2026, 10, 6, 17, 15)), OCT6)

    def test_schedule_on_holiday_skips(self):
        holiday_calendar = [d for d in CALENDAR if d != OCT6]
        self.assertIsNone(choose_report_day({"source": "schedule"}, holiday_calendar, datetime(2026, 10, 6, 17, 15)))

    def test_manual_run_uses_latest_finished_session(self):
        self.assertEqual(choose_report_day({}, CALENDAR, datetime(2026, 10, 6, 11, 0)), OCT5)
        self.assertEqual(choose_report_day({}, CALENDAR, datetime(2026, 10, 10, 9, 0)), OCT9)


class FakeSSM:
    def __init__(self, values, invalid=()):
        self.values, self.invalid, self.calls = values, list(invalid), []

    def get_parameters(self, Names, WithDecryption):
        self.calls.append((Names, WithDecryption))
        return {"Parameters": [{"Name": n, "Value": self.values[n]} for n in Names if n in self.values],
                "InvalidParameters": self.invalid}


class TestLoadSecrets(unittest.TestCase):
    def test_one_call_with_decryption(self):
        ssm = FakeSSM({"/a": "1", "/b": "2"})
        self.assertEqual(load_secrets(ssm, ["/a", "/b"]), {"/a": "1", "/b": "2"})
        self.assertEqual(ssm.calls, [(["/a", "/b"], True)])

    def test_missing_parameter_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            load_secrets(FakeSSM({"/a": "1"}, invalid=["/b"]), ["/a", "/b"])


SECRETS = {"key_id": "KEY", "secret_key": "SECRET", "webhook_url": "https://discord.example/hook"}


class TestRunDaily(unittest.TestCase):
    def setUp(self):
        bars = {"UP": daily_bars(10, 12), "DOWN": daily_bars(10, 8),
                "SPY": daily_bars(670, 675), "QQQ": daily_bars(600, 597)}
        self.client = FakeClient(assets=[asset("UP"), asset("DOWN")], bars=bars)
        self.made_with = []
        self.posted = []
        self.logs = []

    def make_client(self, key_id, secret_key):
        self.made_with.append((key_id, secret_key))
        return self.client

    def poster(self, url, messages):
        self.posted.append((url, messages))
        return [204] * len(messages)

    def run_job(self, event, now):
        return run_daily(event, now, SECRETS, make_client=self.make_client,
                         poster=self.poster, log=self.logs.append)

    def test_scheduled_run_posts_today(self):
        outcome = self.run_job({"source": "schedule"}, utc(2026, 10, 6, 21, 15))
        self.assertEqual(outcome["status"], "posted")
        self.assertEqual(outcome["day"], "2026-10-06")
        self.assertEqual(self.made_with, [("KEY", "SECRET")])
        url, messages = self.posted[0]
        self.assertEqual(url, "https://discord.example/hook")
        self.assertIn("Tue Oct 6, 2026", messages[0])
        self.assertTrue(any("scanned 2" in line for line in self.logs))

    def test_dry_run_never_posts(self):
        outcome = self.run_job({"source": "schedule", "dry_run": True}, utc(2026, 10, 6, 21, 15))
        self.assertEqual(outcome["status"], "dry_run")
        self.assertIn("NantWatch", outcome["preview"])
        self.assertEqual(self.posted, [])

    def test_scheduled_run_on_holiday_skips_quietly(self):
        self.client.calendar = [d for d in CALENDAR if d != OCT6]
        outcome = self.run_job({"source": "schedule"}, utc(2026, 10, 6, 21, 15))
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(self.posted, [])
        self.assertEqual(self.client.bar_calls, [])

    def test_weekend_date_skips(self):
        outcome = self.run_job({"date": "2026-10-10"}, utc(2026, 10, 10, 13, 0))
        self.assertEqual(outcome["status"], "skipped")


class TestHandlerWiring(unittest.TestCase):
    def test_handler_reads_ssm_names_from_env_and_passes_secrets(self):
        env = {"ALPACA_KEY_ID_PARAM": "/k", "ALPACA_SECRET_KEY_PARAM": "/s", "DISCORD_WEBHOOK_PARAM": "/w"}
        fake_boto3 = mock.Mock()
        fake_boto3.client.return_value = FakeSSM({"/k": "KEY", "/s": "SECRET", "/w": "HOOK"})
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {"boto3": fake_boto3}), \
                mock.patch.object(app, "run_daily", return_value={"status": "ok"}) as fake_run:
            self.assertEqual(app.daily_handler({"dry_run": True}, None), {"status": "ok"})
        fake_boto3.client.assert_called_once_with("ssm")
        event, _, secrets = fake_run.call_args[0]
        self.assertEqual(event, {"dry_run": True})
        self.assertEqual(secrets, {"key_id": "KEY", "secret_key": "SECRET", "webhook_url": "HOOK"})


if __name__ == "__main__":
    unittest.main()
