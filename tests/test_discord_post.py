import io
import json
import unittest
import urllib.error
from unittest import mock

from discord_post import build_payload, post_message, post_messages, retry_after_seconds


class TestBuildPayload(unittest.TestCase):
    def test_payload_blocks_all_mentions(self):
        payload = build_payload("hello @everyone")
        self.assertEqual(payload["content"], "hello @everyone")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["username"], "BreakdownBot")

    def test_payload_is_json_serializable(self):
        json.dumps(build_payload("📊 test"))

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            build_payload("   ")

    def test_rejects_over_2000_chars(self):
        with self.assertRaises(ValueError):
            build_payload("x" * 2001)
        build_payload("x" * 2000)  # exactly 2000 is allowed


class TestRetryAfter(unittest.TestCase):
    def test_parses_discord_body(self):
        self.assertEqual(retry_after_seconds(b'{"retry_after": 0.75, "global": false}'), 0.75)

    def test_garbage_uses_default(self):
        self.assertEqual(retry_after_seconds(b"not json"), 1.0)

    def test_clamps_large_values(self):
        self.assertEqual(retry_after_seconds(b'{"retry_after": 999}'), 10.0)


class TestPostMessages(unittest.TestCase):
    def test_sends_in_order_and_pauses_between(self):
        sent, sleeps = [], []

        def fake_sender(url, message):
            sent.append((url, message))
            return 204

        statuses = post_messages(
            "https://example.invalid/hook", ["one", "two", "three"],
            pause_seconds=1.5, sender=fake_sender, sleep=sleeps.append,
        )
        self.assertEqual(statuses, [204, 204, 204])
        self.assertEqual([m for _, m in sent], ["one", "two", "three"])
        self.assertEqual(sleeps, [1.5, 1.5])  # between messages, not after the last

    def test_no_messages_sends_nothing(self):
        self.assertEqual(post_messages("u", [], sender=lambda u, m: 204, sleep=lambda s: None), [])


def http_error(code, body):
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(body))


class TestPostMessageRetries(unittest.TestCase):
    """mock.patch swaps the real network call for a fake one during the test."""

    def test_retries_once_after_429_then_succeeds(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value.status = 204
        fake_urlopen = mock.Mock(side_effect=[http_error(429, b'{"retry_after": 0.5}'), ok])
        sleeps = []
        with mock.patch("discord_post.urllib.request.urlopen", fake_urlopen):
            status = post_message("https://example.invalid/hook", "hi", sleep=sleeps.append)
        self.assertEqual(status, 204)
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(fake_urlopen.call_count, 2)

    def test_other_errors_raise_without_leaking_the_url(self):
        secret_url = "https://discord.com/api/webhooks/123/SECRET-TOKEN"
        fake_urlopen = mock.Mock(side_effect=http_error(400, b'{"message": "bad"}'))
        with mock.patch("discord_post.urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                post_message(secret_url, "hi", sleep=lambda s: None)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
