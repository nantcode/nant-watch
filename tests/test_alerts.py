import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

import app
from app import alert_text, run_with_alert
from formatting import DISCORD_HARD_LIMIT
from test_app import FakeSSM

NOW_UTC = datetime(2026, 10, 9, 21, 15, tzinfo=timezone.utc)   # 5:15 PM in New York
SECRETS = {"key_id": "K", "secret_key": "TOPSECRET", "webhook_url": "https://discord.example/hook"}


class TestAlertText(unittest.TestCase):
    def test_message_contents(self):
        text = alert_text("daily", ValueError("boom"), datetime(2026, 10, 9, 17, 15))
        self.assertIn("NantWatch daily run failed", text)
        self.assertIn("Fri Oct 9, 05:15 PM ET", text)
        self.assertIn("ValueError: boom", text)
        self.assertIn("/aws/lambda/nant-watch-daily", text)
        self.assertEqual(text.count("```"), 2)

    def test_long_errors_are_trimmed_and_backticks_removed(self):
        text = alert_text("weekly", RuntimeError("x" * 5000 + "```"), datetime(2026, 10, 10, 9, 0))
        self.assertLess(len(text), DISCORD_HARD_LIMIT)
        self.assertEqual(text.count("```"), 2)


class TestRunWithAlert(unittest.TestCase):
    def setUp(self):
        self.posted, self.logs = [], []

    def poster(self, url, messages):
        self.posted.append((url, messages))
        return [204]

    def test_success_passes_result_through_without_alert(self):
        result = run_with_alert("daily", lambda e, n, s: {"status": "posted"}, {}, NOW_UTC, SECRETS,
                                poster=self.poster, log=self.logs.append)
        self.assertEqual(result, {"status": "posted"})
        self.assertEqual(self.posted, [])

    def test_failure_posts_alert_then_reraises(self):
        def broken_job(event, now, secrets):
            raise RuntimeError("Alpaca is down")

        with self.assertRaises(RuntimeError):
            run_with_alert("weekly", broken_job, {}, NOW_UTC, SECRETS, poster=self.poster, log=self.logs.append)
        url, messages = self.posted[0]
        self.assertEqual(url, "https://discord.example/hook")
        self.assertIn("NantWatch weekly run failed", messages[0])
        self.assertIn("Alpaca is down", messages[0])
        self.assertNotIn("TOPSECRET", messages[0])
        self.assertTrue(any("ERROR: weekly run failed" in line for line in self.logs))

    def test_broken_discord_does_not_hide_the_real_error(self):
        def broken_poster(url, messages):
            raise ConnectionError("discord unreachable")

        def broken_job(event, now, secrets):
            raise KeyError("missing field")

        with self.assertRaises(KeyError):
            run_with_alert("daily", broken_job, {}, NOW_UTC, SECRETS, poster=broken_poster, log=self.logs.append)
        self.assertTrue(any("could not post the failure alert" in line for line in self.logs))


class TestHandlersAlert(unittest.TestCase):
    ENV = {"ALPACA_KEY_ID_PARAM": "/k", "ALPACA_SECRET_KEY_PARAM": "/s", "DISCORD_WEBHOOK_PARAM": "/w"}

    def test_daily_handler_crash_alerts_discord_and_fails_the_lambda(self):
        fake_boto3 = mock.Mock()
        fake_boto3.client.return_value = FakeSSM({"/k": "K", "/s": "S", "/w": "HOOK"})
        with mock.patch.dict(os.environ, self.ENV), mock.patch.dict(sys.modules, {"boto3": fake_boto3}), \
                mock.patch.object(app, "run_daily", side_effect=ValueError("bad date")), \
                mock.patch.object(app, "post_messages", return_value=[204]) as fake_post, \
                mock.patch("builtins.print"):   # keep the test output quiet
            with self.assertRaises(ValueError):
                app.daily_handler({"date": "not-a-date"}, None)
        url, messages = fake_post.call_args[0]
        self.assertEqual(url, "HOOK")
        self.assertIn("NantWatch daily run failed", messages[0])

    def test_bad_date_really_raises_inside_run_daily(self):
        with self.assertRaises(ValueError):
            app.choose_report_day({"date": "not-a-date"}, [], datetime(2026, 10, 9, 17, 15))


if __name__ == "__main__":
    unittest.main()
