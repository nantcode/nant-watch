import unittest
from datetime import date, datetime, timedelta

from alpaca_client import AlpacaError
from formatting import SAFE_LIMIT
from market_math import Window
from scanner import (
    build_daily,
    index_movers,
    pick_report_day,
    run_scan,
    scan_market,
)

OCT2, OCT5, OCT6, OCT7, OCT10 = (date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6),
                                 date(2026, 10, 7), date(2026, 10, 10))


def weekdays(start, end):
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


CALENDAR = weekdays(date(2026, 9, 14), date(2026, 10, 16))


def bar(day, close, volume=1_000_000):
    return {"t": day.isoformat() + "T04:00:00Z", "c": close, "v": volume}


def asset(symbol, exchange="NASDAQ"):
    return {"symbol": symbol, "name": symbol + " Inc", "class": "us_equity",
            "exchange": exchange, "status": "active", "tradable": True}


class FakeClient:
    """Stands in for AlpacaClient: same method names, canned answers, no network."""

    def __init__(self, calendar=CALENDAR, assets=(), bars=None, fail_symbols=()):
        self.calendar = list(calendar)
        self.assets = list(assets)
        self.bars = bars or {}
        self.fail_symbols = set(fail_symbols)
        self.request_count = 0
        self.bar_calls = []

    def get_calendar(self, start, end):
        self.request_count += 1
        return [{"date": d.isoformat()} for d in self.calendar if start <= d <= end]

    def get_assets(self):
        self.request_count += 1
        return self.assets

    def get_bars(self, symbols, start, end=None):
        self.request_count += 1
        self.bar_calls.append((list(symbols), start))
        if self.fail_symbols & set(symbols):
            raise AlpacaError("HTTP 400: invalid symbol")
        return {s: self.bars[s] for s in symbols if s in self.bars}


def daily_bars(prev_close, close, volume=1_000_000):
    return [bar(OCT5, prev_close, volume), bar(OCT6, close, volume)]


class TestPickReportDay(unittest.TestCase):
    def test_after_close_on_trading_day_is_today(self):
        self.assertEqual(pick_report_day(CALENDAR, datetime(2026, 10, 6, 17, 0)), OCT6)

    def test_before_close_uses_previous_session(self):
        self.assertEqual(pick_report_day(CALENDAR, datetime(2026, 10, 6, 11, 0)), OCT5)

    def test_weekend_uses_friday(self):
        self.assertEqual(pick_report_day(CALENDAR, datetime(2026, 10, 10, 9, 0)), date(2026, 10, 9))

    def test_no_calendar(self):
        self.assertIsNone(pick_report_day([], datetime(2026, 10, 6, 17, 0)))


class TestScanMarket(unittest.TestCase):
    def setUp(self):
        self.window = Window(OCT5, (OCT6,))

    def test_batches_filters_and_progress(self):
        bars = {"AAA": daily_bars(10, 11), "BBB": daily_bars(10, 9), "CCC": daily_bars(2, 3),
                "DDD": daily_bars(10, 12, volume=10), "EEE": daily_bars(20, 20)}
        client = FakeClient(bars=bars)
        progress = []
        movers, failed = scan_market(client, self.window, ["AAA", "BBB", "CCC", "DDD", "EEE"],
                                     batch_size=2, on_batch=lambda n, t: progress.append((n, t)))
        self.assertEqual(sorted(m.symbol for m in movers), ["AAA", "BBB", "EEE"])  # CCC cheap, DDD thin
        self.assertEqual(failed, 0)
        self.assertEqual(len(client.bar_calls), 3)
        self.assertEqual(client.bar_calls[0][1], OCT5)   # bars start at the base day
        self.assertEqual(progress, [(1, 3), (2, 3), (3, 3)])

    def test_one_bad_batch_is_skipped(self):
        client = FakeClient(bars={"AAA": daily_bars(10, 11), "CCC": daily_bars(10, 12)}, fail_symbols={"BAD"})
        movers, failed = scan_market(client, self.window, ["AAA", "BAD", "CCC"], batch_size=1)
        self.assertEqual(failed, 1)
        self.assertEqual(sorted(m.symbol for m in movers), ["AAA", "CCC"])

    def test_all_batches_failing_raises(self):
        client = FakeClient(fail_symbols={"A", "B"})
        with self.assertRaises(AlpacaError):
            scan_market(client, self.window, ["A", "B"], batch_size=1)


class TestIndexMovers(unittest.TestCase):
    def test_fixed_order_and_no_filters(self):
        bars = {"QQQ": daily_bars(600, 597), "SPY": daily_bars(670, 675, volume=1)}
        movers = index_movers(FakeClient(bars=bars), Window(OCT5, (OCT6,)))
        self.assertEqual([m.symbol for m in movers], ["SPY", "QQQ"])


class TestBuildDaily(unittest.TestCase):
    def make_market(self):
        bars, assets = {}, []
        for i in range(30):
            symbol = f"UP{i:02d}"
            assets.append(asset(symbol))
            bars[symbol] = daily_bars(10, 10 + (i + 1) * 0.1)
        for i in range(30):
            symbol = f"DN{i:02d}"
            assets.append(asset(symbol))
            bars[symbol] = daily_bars(10, 10 - (i + 1) * 0.1)
        assets.append(asset("JUNK", exchange="OTC"))
        bars["JUNK"] = daily_bars(10, 50)
        bars["SPY"] = daily_bars(670, 675.6, volume=60_000_000)
        bars["QQQ"] = daily_bars(600, 597.8, volume=40_000_000)
        return FakeClient(assets=assets, bars=bars)

    def test_weekend_returns_none_without_scanning(self):
        client = self.make_market()
        self.assertIsNone(build_daily(client, OCT10))
        self.assertEqual(client.bar_calls, [])

    def test_full_daily_report(self):
        client = self.make_market()
        result, messages = build_daily(client, OCT6)
        self.assertEqual(result.window, Window(OCT5, (OCT6,)))
        self.assertEqual(result.symbols_scanned, 60)          # JUNK (OTC) never scanned
        self.assertEqual([m.symbol for m in result.gainers][:2], ["UP29", "UP28"])
        self.assertEqual([m.symbol for m in result.losers][:2], ["DN29", "DN28"])
        self.assertEqual(len(result.gainers), 10)
        self.assertEqual([m.symbol for m in result.indexes], ["SPY", "QQQ"])
        self.assertEqual(len(messages), 1)                    # top 10 + top 10 fits one post
        self.assertLessEqual(len(messages[0]), SAFE_LIMIT)
        self.assertIn("Tue Oct 6, 2026", messages[0])
        self.assertNotIn("JUNK", messages[0])
        self.assertIn("scanned 60", result.summary())

    def test_run_scan_reports_failed_batches(self):
        client = self.make_market()
        client.fail_symbols = {"UP00"}
        result = run_scan(client, Window(OCT5, (OCT6,)), clock=iter([0.0, 2.5]).__next__, batch_size=10)
        self.assertEqual(result.failed_batches, 1)          # 6 batches of 10, one bad
        self.assertEqual(result.symbols_kept, 50)
        self.assertEqual(result.seconds, 2.5)


if __name__ == "__main__":
    unittest.main()
