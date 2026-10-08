import unittest
from datetime import date, datetime, timezone

from alpaca_client import TRADING_BASE, AlpacaError
from formatting import CODE_FENCE, SAFE_LIMIT
from race import (
    Racer,
    build_scoreboard,
    close_series,
    collect_racers,
    equity_series,
    format_scoreboard,
    race_base_day,
    racer_from_series,
    rank_racers,
)
from test_alpaca_client import make_client
from test_scanner import CALENDAR

OCT1, OCT2, OCT5, OCT6, OCT9 = (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
                                date(2026, 10, 6), date(2026, 10, 9))


def stamp(day):
    """Alpaca stamps daily points at midnight New York time (04:00 UTC in October)."""
    return int(datetime(day.year, day.month, day.day, 4, tzinfo=timezone.utc).timestamp())


def history(points):
    return {"timestamp": [stamp(d) for d, _ in points], "equity": [v for _, v in points]}


def bars(points):
    return [{"t": d.isoformat() + "T04:00:00Z", "c": c, "v": 1} for d, c in points]


class FakeAccount:
    def __init__(self, points=None, error=False):
        self.points, self.error = points or [], error

    def get_portfolio_history(self):
        if self.error:
            raise AlpacaError("HTTP 403: forbidden")
        return history(self.points)


class FakeMarket:
    def __init__(self, spy_points):
        self.spy_points = spy_points
        self.calls = []

    def get_bars(self, symbols, start, end=None):
        self.calls.append((list(symbols), start))
        return {"SPY": bars(self.spy_points)} if self.spy_points else {}


class TestSeries(unittest.TestCase):
    def test_base_day_is_friday_before_race(self):
        self.assertEqual(race_base_day(CALENDAR), OCT2)
        self.assertIsNone(race_base_day([OCT5, OCT6]))

    def test_equity_series_drops_empty_days_and_sorts(self):
        raw = history([(OCT5, 101.0), (OCT1, None), (OCT2, 100.0), (OCT6, 0)])
        self.assertEqual(equity_series(raw), [(OCT2, 100.0), (OCT5, 101.0)])

    def test_close_series(self):
        self.assertEqual(close_series(bars([(OCT5, 2.0), (OCT2, 1.0)])), [(OCT2, 1.0), (OCT5, 2.0)])

    def test_racer_uses_base_day_close_as_start(self):
        racer = racer_from_series("Bot", [(OCT1, 90.0), (OCT2, 100.0), (OCT5, 103.0), (OCT9, 110.0)], OCT2)
        self.assertEqual((racer.start_value, racer.end_value, racer.end_day), (100.0, 110.0, OCT9))
        self.assertAlmostEqual(racer.return_pct, 10.0)
        self.assertAlmostEqual(racer.stake_now, 11_000.0)

    def test_brand_new_account_starts_at_first_value(self):
        racer = racer_from_series("New", [(OCT5, 100_000.0), (OCT6, 99_000.0)], OCT2)
        self.assertEqual(racer.start_value, 100_000.0)
        self.assertAlmostEqual(racer.return_pct, -1.0)

    def test_no_data(self):
        self.assertIsNone(racer_from_series("Ghost", [], OCT2))


class TestFormatting(unittest.TestCase):
    def setUp(self):
        self.racers = [Racer("NantBot", 100_000, 99_100, OCT9),
                       Racer("SPY", 670.0, 673.685, OCT9),
                       Racer("Sparticus", 100_000, 101_230, OCT9)]

    def test_rank(self):
        self.assertEqual([r.name for r in rank_racers(self.racers)], ["Sparticus", "SPY", "NantBot"])

    def test_scoreboard_text(self):
        text = format_scoreboard(self.racers, "Mon Oct 5, 2026", "Fri Oct 9, 2026")
        lines = text.splitlines()
        self.assertIn("since Mon Oct 5, 2026 · as of Fri Oct 9, 2026", lines[0])
        self.assertTrue(lines[3].startswith("🥇 Sparticus"))
        self.assertIn("+1.23%", lines[3])
        self.assertIn("$10,123.00", lines[3])
        self.assertTrue(lines[5].startswith("🥉 NantBot"))
        self.assertIn("Sparticus is beating SPY by 0.68 pts", text)
        self.assertIn("NantBot is trailing SPY by 1.45 pts", text)
        self.assertEqual(text.count(CODE_FENCE), 2)
        self.assertLessEqual(len(text), SAFE_LIMIT)

    def test_missing_racers_and_empty_board(self):
        text = format_scoreboard(self.racers[:2], "a", "b", missing=["Sparticus"])
        self.assertIn("Sparticus: no data yet", text)
        self.assertIn("(no data yet)", format_scoreboard([], "a", "b"))

    def test_tie_wording(self):
        text = format_scoreboard([Racer("Bot", 100, 101, OCT9), Racer("SPY", 50, 50.5, OCT9)], "a", "b")
        self.assertIn("Bot is tied with SPY", text)


class TestCollect(unittest.TestCase):
    def test_full_race(self):
        accounts = {"NantBot": FakeAccount([(OCT2, 100_000), (OCT9, 99_100)]),
                    "Sparticus": FakeAccount([(OCT2, 100_000), (OCT9, 101_230)])}
        market = FakeMarket([(OCT2, 670.0), (OCT9, 673.685)])
        racers, missing, base_day = collect_racers(accounts, market, CALENDAR)
        self.assertEqual(base_day, OCT2)
        self.assertEqual(sorted(r.name for r in racers), ["NantBot", "SPY", "Sparticus"])
        self.assertEqual(missing, [])
        self.assertEqual(market.calls, [(["SPY"], OCT2)])

    def test_missing_keys_and_api_errors_become_missing(self):
        accounts = {"NantBot": FakeAccount([(OCT2, 100_000), (OCT9, 99_100)]),
                    "Sparticus": None, "Broken": FakeAccount(error=True)}
        racers, missing, _ = collect_racers(accounts, FakeMarket([]), CALENDAR)
        self.assertEqual([r.name for r in racers], ["NantBot"])
        self.assertEqual(missing, ["Sparticus", "Broken", "SPY"])

    def test_build_scoreboard(self):
        accounts = {"NantBot": FakeAccount([(OCT2, 100_000), (OCT9, 101_000)])}
        text, racers = build_scoreboard(accounts, FakeMarket([(OCT2, 670.0), (OCT9, 670.0)]), CALENDAR)
        self.assertIn("as of Fri Oct 9, 2026", text)
        self.assertEqual(len(racers), 2)

    def test_calendar_too_short(self):
        with self.assertRaises(ValueError):
            collect_racers({}, FakeMarket([]), [OCT5, OCT6])


class TestPortfolioHistoryRequest(unittest.TestCase):
    def test_read_only_get_with_daily_points(self):
        client, transport, _ = make_client([(200, {"timestamp": [], "equity": []})])
        client.get_portfolio_history()
        self.assertEqual(transport.calls[0][0],
                         TRADING_BASE + "/v2/account/portfolio/history?period=1A&timeframe=1D")


if __name__ == "__main__":
    unittest.main()
