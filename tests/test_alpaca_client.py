import io
import json
import unittest
import urllib.error
from datetime import date
from unittest import mock

from alpaca_client import (
    DATA_BASE,
    TRADING_BASE,
    AlpacaClient,
    AlpacaError,
    RateLimiter,
    ReadOnlyViolation,
    build_url,
    check_read_only,
    chunked,
    urllib_transport,
)


class FakeTransport:
    """Pretends to be Alpaca: hands back queued (status, payload) answers
    and records every URL + headers it was called with."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        status, payload = self.responses.pop(0)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, body


def make_client(responses):
    transport = FakeTransport(responses)
    sleeps = []
    never_limits = RateLimiter(max_calls=1000, clock=lambda: 0.0, sleep=sleeps.append)
    client = AlpacaClient("KEYID", "SUPERSECRET", transport=transport,
                          limiter=never_limits, sleep=sleeps.append)
    return client, transport, sleeps


class TestReadOnlyGuard(unittest.TestCase):
    def test_allowed_reads_pass(self):
        check_read_only("GET", TRADING_BASE + "/v2/assets?status=active")
        check_read_only("GET", TRADING_BASE + "/v2/calendar")
        check_read_only("GET", DATA_BASE + "/v2/stocks/bars?symbols=SPY")
        check_read_only("GET", TRADING_BASE + "/v2/account/portfolio/history")
        check_read_only("get", TRADING_BASE + "/v2/assets")

    def test_writes_are_blocked(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(ReadOnlyViolation):
                check_read_only(method, TRADING_BASE + "/v2/assets")

    def test_orders_and_positions_blocked_even_for_get(self):
        for path in ("/v2/orders", "/v2/positions", "/v2/assets/../orders", "/V2/ORDERS"):
            with self.assertRaises(ReadOnlyViolation):
                check_read_only("GET", TRADING_BASE + path)

    def test_paths_not_on_allowlist_blocked(self):
        for path in ("/v2/account", "/v2/account/configurations", "/v2/watchlists"):
            with self.assertRaises(ReadOnlyViolation):
                check_read_only("GET", TRADING_BASE + path)

    def test_transport_can_only_send_get(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["method"] = request.get_method()
            response = mock.MagicMock()
            response.__enter__.return_value.status = 200
            response.__enter__.return_value.read.return_value = b"[]"
            return response

        with mock.patch("alpaca_client.urllib.request.urlopen", fake_urlopen):
            self.assertEqual(urllib_transport("https://example.invalid/v2/assets", {}), (200, b"[]"))
        self.assertEqual(seen["method"], "GET")

    def test_transport_returns_http_errors_instead_of_raising(self):
        error = urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b'{"message":"no"}'))
        with mock.patch("alpaca_client.urllib.request.urlopen", side_effect=error):
            self.assertEqual(urllib_transport("https://example.invalid/v2/assets", {}), (403, b'{"message":"no"}'))


class TestHelpers(unittest.TestCase):
    def test_build_url_drops_none_and_encodes(self):
        url = build_url(DATA_BASE, "/v2/stocks/bars", {"symbols": "A,B", "end": None, "limit": 5})
        self.assertEqual(url, DATA_BASE + "/v2/stocks/bars?symbols=A%2CB&limit=5")
        self.assertEqual(build_url(DATA_BASE, "/x"), DATA_BASE + "/x")

    def test_chunked(self):
        self.assertEqual(chunked(["A", "B", "C", "D", "E"], 2), [["A", "B"], ["C", "D"], ["E"]])
        self.assertEqual(chunked([], 3), [])
        with self.assertRaises(ValueError):
            chunked(["A"], 0)


class TestRateLimiter(unittest.TestCase):
    def test_waits_only_when_window_is_full(self):
        now = [0.0]
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        limiter = RateLimiter(max_calls=3, period=60.0, clock=lambda: now[0], sleep=fake_sleep)
        for t in (0.0, 1.0, 2.0):
            now[0] = t
            limiter.wait()
        self.assertEqual(sleeps, [])           # first 3 calls are free
        now[0] = 3.0
        limiter.wait()
        self.assertEqual(sleeps, [57.0])       # 4th waits until the call at t=0 is 60s old
        limiter.wait()
        self.assertEqual(sleeps, [57.0, 1.0])  # then until the call at t=1 expires

    def test_old_calls_expire(self):
        now = [0.0]
        sleeps = []
        limiter = RateLimiter(max_calls=1, period=60.0, clock=lambda: now[0], sleep=sleeps.append)
        limiter.wait()
        now[0] = 61.0
        limiter.wait()
        self.assertEqual(sleeps, [])


class TestClient(unittest.TestCase):
    def test_requires_keys(self):
        with self.assertRaises(ValueError):
            AlpacaClient("", "secret")

    def test_get_assets_url_and_headers(self):
        client, transport, _ = make_client([(200, [{"symbol": "AAPL"}])])
        self.assertEqual(client.get_assets(), [{"symbol": "AAPL"}])
        url, headers = transport.calls[0]
        self.assertEqual(url, TRADING_BASE + "/v2/assets?status=active&asset_class=us_equity")
        self.assertEqual(headers["APCA-API-KEY-ID"], "KEYID")
        self.assertEqual(headers["APCA-API-SECRET-KEY"], "SUPERSECRET")

    def test_get_calendar_dates(self):
        client, transport, _ = make_client([(200, [{"date": "2026-10-05"}])])
        client.get_calendar(date(2026, 10, 1), date(2026, 10, 9))
        self.assertIn("start=2026-10-01&end=2026-10-09", transport.calls[0][0])

    def test_get_bars_follows_pages_and_merges(self):
        page1 = {"bars": {"AAA": [{"t": "2026-10-05T04:00:00Z", "c": 1}]}, "next_page_token": "TOKEN2"}
        page2 = {"bars": {"AAA": [{"t": "2026-10-06T04:00:00Z", "c": 2}],
                          "BBB": [{"t": "2026-10-06T04:00:00Z", "c": 3}]}, "next_page_token": None}
        client, transport, _ = make_client([(200, page1), (200, page2)])
        bars = client.get_bars(["AAA", "BBB"], date(2026, 10, 5))
        self.assertEqual(len(bars["AAA"]), 2)
        self.assertEqual(len(bars["BBB"]), 1)
        first_url, second_url = transport.calls[0][0], transport.calls[1][0]
        for expected in ("symbols=AAA%2CBBB", "timeframe=1Day", "feed=sip", "adjustment=split", "start=2026-10-05"):
            self.assertIn(expected, first_url)
        self.assertNotIn("end=", first_url)       # let Alpaca pick a safe end time
        self.assertNotIn("page_token", first_url)
        self.assertIn("page_token=TOKEN2", second_url)

    def test_empty_bars_response(self):
        client, _, _ = make_client([(200, {"bars": None, "next_page_token": None})])
        self.assertEqual(client.get_bars(["ZZZ"], date(2026, 10, 5)), {})

    def test_batched_makes_one_call_per_batch(self):
        responses = [(200, {"bars": {s: [{"c": 1}]}, "next_page_token": None}) for s in ("A", "C", "E")]
        client, transport, _ = make_client(responses)
        bars = client.get_bars_batched(["A", "B", "C", "D", "E"], date(2026, 10, 5), batch_size=2)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sorted(bars), ["A", "C", "E"])
        self.assertEqual(client.request_count, 3)

    def test_retries_429_with_backoff(self):
        client, transport, sleeps = make_client([(429, b"slow down"), (200, [])])
        self.assertEqual(client.get_assets(), [])
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(client.request_count, 2)

    def test_gives_up_after_max_attempts(self):
        client, transport, sleeps = make_client([(500, b"oops")] * 4)
        with self.assertRaises(AlpacaError):
            client.get_assets()
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(sleeps, [2.0, 4.0, 6.0])

    def test_auth_error_is_not_retried_and_hides_secret(self):
        client, transport, _ = make_client([(403, b'{"message": "bad key SUPERSECRET"}')])
        with self.assertRaises(AlpacaError) as ctx:
            client.get_assets()
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertNotIn("SUPERSECRET", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
