import unittest
from datetime import date, timedelta

from formatting import Mover
from market_math import (
    Bar,
    Window,
    bar_from_alpaca,
    compute_mover,
    daily_window,
    eligible_symbols,
    is_eligible_asset,
    movers_from_bars,
    parse_day,
    pct_change,
    rank_movers,
    trading_days_from_calendar,
    weekly_window,
)


def weekdays(start, end, skip=()):
    """All Mon-Fri dates from start to end, minus any 'holidays' in skip."""
    days, day = [], start
    while day <= end:
        if day.weekday() < 5 and day not in skip:
            days.append(day)
        day += timedelta(days=1)
    return days


SEP28, OCT2, OCT5, OCT6, OCT9, OCT10 = (
    date(2026, 9, 28), date(2026, 10, 2), date(2026, 10, 5),
    date(2026, 10, 6), date(2026, 10, 9), date(2026, 10, 10),
)
CALENDAR = weekdays(SEP28, date(2026, 10, 16))


def stock(**overrides):
    asset = {"symbol": "ACME", "name": "Acme Corp Common Stock", "class": "us_equity",
             "exchange": "NASDAQ", "status": "active", "tradable": True}
    asset.update(overrides)
    return asset


class TestParsing(unittest.TestCase):
    def test_parse_day_handles_both_formats(self):
        self.assertEqual(parse_day("2026-10-05"), OCT5)
        self.assertEqual(parse_day("2026-10-05T04:00:00Z"), OCT5)

    def test_bar_from_alpaca(self):
        raw = {"t": "2026-10-05T04:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1200, "n": 5, "vw": 10.2}
        self.assertEqual(bar_from_alpaca(raw), Bar(OCT5, 10.5, 1200.0))

    def test_calendar_sorted_and_unique(self):
        raw = [{"date": "2026-10-06"}, {"date": "2026-10-05"}, {"date": "2026-10-06"}]
        self.assertEqual(trading_days_from_calendar(raw), [OCT5, OCT6])


class TestDailyWindow(unittest.TestCase):
    def test_trading_day_compares_to_previous_session(self):
        self.assertEqual(daily_window(CALENDAR, OCT6), Window(OCT5, (OCT6,)))

    def test_monday_compares_to_friday(self):
        self.assertEqual(daily_window(CALENDAR, OCT5).base_day, OCT2)

    def test_weekend_returns_none(self):
        self.assertIsNone(daily_window(CALENDAR, OCT10))

    def test_holiday_returns_none_and_next_day_skips_it(self):
        cal = weekdays(SEP28, OCT9, skip={OCT5})
        self.assertIsNone(daily_window(cal, OCT5))
        self.assertEqual(daily_window(cal, OCT6).base_day, OCT2)

    def test_no_history_returns_none(self):
        self.assertIsNone(daily_window([OCT5], OCT5))


class TestWeeklyWindow(unittest.TestCase):
    def test_saturday_covers_mon_to_fri_from_prior_friday(self):
        window = weekly_window(CALENDAR, OCT10)
        self.assertEqual(window.base_day, OCT2)
        self.assertEqual(window.days, tuple(weekdays(OCT5, OCT9)))
        self.assertEqual(window.end_day, OCT9)

    def test_monday_holiday_week(self):
        cal = weekdays(SEP28, OCT9, skip={OCT5})
        window = weekly_window(cal, OCT10)
        self.assertEqual(window.base_day, OCT2)
        self.assertEqual(len(window.days), 4)

    def test_friday_holiday_ends_thursday(self):
        cal = weekdays(SEP28, OCT9, skip={OCT9})
        self.assertEqual(weekly_window(cal, OCT10).end_day, date(2026, 10, 8))

    def test_not_enough_history(self):
        self.assertIsNone(weekly_window(weekdays(OCT5, OCT9), OCT10))
        self.assertIsNone(weekly_window([], OCT10))


class TestEligibility(unittest.TestCase):
    def test_normal_stock_is_eligible(self):
        self.assertTrue(is_eligible_asset(stock()))

    def test_rejections(self):
        self.assertFalse(is_eligible_asset(stock(exchange="OTC")))
        self.assertFalse(is_eligible_asset(stock(status="inactive")))
        self.assertFalse(is_eligible_asset(stock(tradable=False)))
        self.assertFalse(is_eligible_asset(stock(**{"class": "crypto"})))
        self.assertFalse(is_eligible_asset(stock(name="Acme Acquisition Corp Warrants")))
        self.assertFalse(is_eligible_asset(stock(name="Acme Acquisition Corp Units")))
        self.assertFalse(is_eligible_asset(stock(name="Acme Rights")))

    def test_word_boundary_keeps_united(self):
        self.assertTrue(is_eligible_asset(stock(name="United Airlines Holdings Inc")))

    def test_eligible_symbols_sorted_unique(self):
        assets = [stock(symbol="ZZZ"), stock(symbol="AAA"), stock(symbol="AAA"),
                  stock(symbol="OTCX", exchange="OTC")]
        self.assertEqual(eligible_symbols(assets), ["AAA", "ZZZ"])


class TestMath(unittest.TestCase):
    def test_pct_change(self):
        self.assertAlmostEqual(pct_change(100, 103), 3.0)
        self.assertAlmostEqual(pct_change(50, 25), -50.0)

    def test_pct_change_rejects_zero_start(self):
        with self.assertRaises(ValueError):
            pct_change(0, 10)


class TestComputeMover(unittest.TestCase):
    def setUp(self):
        self.window = Window(OCT2, tuple(weekdays(OCT5, OCT9)))
        self.bars = [Bar(OCT2, 10.0, 0)] + [Bar(d, 10.0 + i, 1_000_000) for i, d in enumerate(weekdays(OCT5, OCT9), 1)]

    def test_weekly_mover(self):
        mover = compute_mover("ACME", self.bars, self.window)
        self.assertEqual(mover.symbol, "ACME")
        self.assertEqual(mover.price, 15.0)
        self.assertAlmostEqual(mover.change_pct, 50.0)
        self.assertEqual(mover.volume, 1_000_000)  # base-day volume is NOT averaged in

    def test_missing_end_bar_is_skipped(self):
        self.assertIsNone(compute_mover("ACME", self.bars[:-1], self.window))

    def test_missing_base_bar_is_skipped(self):
        self.assertIsNone(compute_mover("ACME", self.bars[1:], self.window))

    def test_penny_stock_filtered_at_start_or_end(self):
        cheap_start = [Bar(OCT2, 4.0, 0)] + self.bars[1:]
        self.assertIsNone(compute_mover("ACME", cheap_start, self.window))
        crashed = self.bars[:-1] + [Bar(OCT9, 4.99, 1_000_000)]
        self.assertIsNone(compute_mover("ACME", crashed, self.window))

    def test_thin_volume_filtered_and_missing_days_count_as_zero(self):
        sparse = [self.bars[0], self.bars[1], self.bars[-1]]  # traded only 2 of 5 days
        self.assertIsNone(compute_mover("ACME", sparse, self.window))  # avg 400k < 500k

    def test_filters_can_be_turned_off_for_indexes(self):
        sparse = [self.bars[0], self.bars[-1]]
        mover = compute_mover("SPY", sparse, self.window, min_price=0, min_avg_volume=0)
        self.assertAlmostEqual(mover.change_pct, 50.0)


class TestMoversFromBars(unittest.TestCase):
    def test_alpaca_shaped_input(self):
        window = Window(OCT5, (OCT6,))
        response = {
            "UPPY": [{"t": "2026-10-05T04:00:00Z", "c": 20, "v": 900_000},
                     {"t": "2026-10-06T04:00:00Z", "c": 25, "v": 900_000}],
            "TINY": [{"t": "2026-10-05T04:00:00Z", "c": 2, "v": 9_000_000},
                     {"t": "2026-10-06T04:00:00Z", "c": 3, "v": 9_000_000}],
        }
        movers = movers_from_bars(response, window)
        self.assertEqual([m.symbol for m in movers], ["UPPY"])
        self.assertAlmostEqual(movers[0].change_pct, 25.0)


class TestRanking(unittest.TestCase):
    def test_rank_order_ties_and_flat(self):
        movers = [Mover("B", 10, 5.0, 1), Mover("A", 10, 5.0, 1), Mover("C", 10, 9.0, 1),
                  Mover("D", 10, -2.0, 1), Mover("E", 10, -8.0, 1), Mover("F", 10, 0.0, 1)]
        gainers, losers = rank_movers(movers)
        self.assertEqual([m.symbol for m in gainers], ["C", "A", "B"])
        self.assertEqual([m.symbol for m in losers], ["E", "D"])

    def test_top_n_limit(self):
        movers = [Mover(f"S{i:02d}", 10, float(i), 1) for i in range(1, 30)]
        gainers, losers = rank_movers(movers, top_n=10)
        self.assertEqual(len(gainers), 10)
        self.assertEqual(gainers[0].symbol, "S29")
        self.assertEqual(losers, [])


if __name__ == "__main__":
    unittest.main()
