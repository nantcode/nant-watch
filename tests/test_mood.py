import unittest
from datetime import date, timedelta

from alpaca_client import AlpacaError
from mood import (
    FUNDS,
    HISTORY_DAYS,
    bars_up_to,
    closes_from_bars,
    count_uptrends,
    fetch_mood_line,
    mood_label,
    mood_line,
    safe_mood_line,
)
from test_scanner import FakeClient, bar, weekdays

END = date(2026, 10, 7)
LAST_220_DAYS = weekdays(END - timedelta(days=330), END)[-220:]
TEN_OF_FOURTEEN = "Market mood: 10 of 14 funds above their 200-day average (bullish)"


def fund_bars(up_count=10, days=LAST_220_DAYS):
    """Alpaca-style bars for the 14 funds: the first `up_count` climbing, the rest sliding."""
    bars = {}
    for k, fund in enumerate(FUNDS):
        closes = [100 + n * 0.5 for n in range(len(days))] if k < up_count else [300 - n * 0.5 for n in range(len(days))]
        bars[fund] = [bar(day, close) for day, close in zip(days, closes)]
    return bars


def rising(n=260, step=0.5):
    return [100 + i * step for i in range(n)]


def falling(n=260, step=0.2):
    return [200 - i * step for i in range(n)]


class TestCountUptrends(unittest.TestCase):

    def test_counts_only_the_funds_above_their_average(self):
        closes = {"A": rising(), "B": rising(), "C": rising(), "D": falling(), "E": falling()}
        self.assertEqual(count_uptrends(closes), 3)

    def test_every_fund_rising_counts_every_fund(self):
        self.assertEqual(count_uptrends({f: rising() for f in FUNDS}), 14)

    def test_every_fund_falling_counts_none(self):
        self.assertEqual(count_uptrends({f: falling() for f in FUNDS}), 0)

    def test_a_fund_sitting_exactly_on_its_average_is_not_counted(self):
        self.assertEqual(count_uptrends({"FLAT": [50.0] * 200}), 0)

    def test_a_fund_with_too_little_history_is_not_counted(self):
        self.assertEqual(count_uptrends({"NEW": rising(199), "OLD": rising(200)}), 1)

    def test_only_the_last_200_closes_matter(self):
        ancient_highs = [9999.0] * 300              # prices from long ago must not drag the average up
        self.assertEqual(count_uptrends({"A": ancient_highs + rising(200)}), 1)

    def test_days_can_be_changed(self):
        dip_after_a_long_climb = rising(200) + [198.0] * 30
        self.assertEqual(count_uptrends({"A": dip_after_a_long_climb}, days=200), 1)   # still above the slow average
        self.assertEqual(count_uptrends({"A": dip_after_a_long_climb}, days=20), 0)    # not above the fast one

    def test_no_funds_at_all_is_zero(self):
        self.assertEqual(count_uptrends({}), 0)


class TestMoodWords(unittest.TestCase):

    def test_there_are_14_funds(self):
        self.assertEqual(len(FUNDS), 14)
        self.assertEqual(len(set(FUNDS)), 14)

    def test_labels(self):
        self.assertEqual(mood_label(10, 14), "bullish")       # 71%
        self.assertEqual(mood_label(9, 14), "mixed")          # 64%
        self.assertEqual(mood_label(5, 14), "mixed")          # 36%
        self.assertEqual(mood_label(4, 14), "bearish")        # 29%
        self.assertEqual(mood_label(0, 0), "unknown")

    def test_the_line_for_the_post(self):
        closes = {f: (rising() if k < 10 else falling()) for k, f in enumerate(FUNDS)}
        self.assertEqual(mood_line(closes), "Market mood: 10 of 14 funds above their 200-day average (bullish)")

    def test_the_line_only_counts_funds_it_could_judge(self):
        closes = {"A": rising(), "B": falling(), "NEW": rising(50)}
        self.assertEqual(mood_line(closes), "Market mood: 1 of 2 funds above their 200-day average (mixed)")


class TestClosesFromBars(unittest.TestCase):
    """TASK A: these pass once closes_from_bars is written."""

    def test_turns_each_funds_bars_into_its_closing_prices(self):
        d1, d2 = date(2026, 10, 5), date(2026, 10, 6)
        bars = {"SPY": [bar(d1, 670.0), bar(d2, 671.5)], "QQQ": [bar(d1, 600.0)]}
        self.assertEqual(closes_from_bars(bars), {"SPY": [670.0, 671.5], "QQQ": [600.0]})

    def test_keeps_the_closes_in_the_order_of_the_bars(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 7))
        bars = {"A": [bar(days[0], 3.0), bar(days[1], 1.0), bar(days[2], 2.0)]}
        self.assertEqual(closes_from_bars(bars), {"A": [3.0, 1.0, 2.0]})       # not sorted by price

    def test_a_fund_with_no_bars_still_appears_with_an_empty_list(self):
        self.assertEqual(closes_from_bars({"NEW": []}), {"NEW": []})

    def test_nothing_in_nothing_out(self):
        self.assertEqual(closes_from_bars({}), {})

    def test_all_14_funds_come_through(self):
        closes = closes_from_bars(fund_bars())
        self.assertEqual(sorted(closes), sorted(FUNDS))
        self.assertTrue(all(len(prices) == 220 for prices in closes.values()))
        self.assertEqual(closes["SPY"][:2], [100.0, 100.5])


class TestFetchMoodLine(unittest.TestCase):
    """These need TASK A too: they run the whole path from Alpaca's bars to the line."""

    def test_the_line_from_alpaca_bars(self):
        self.assertEqual(fetch_mood_line(FakeClient(bars=fund_bars()), END), TEN_OF_FOURTEEN)

    def test_asks_alpaca_once_for_the_14_funds_with_dividend_adjusted_prices(self):
        client = FakeClient(bars=fund_bars())
        fetch_mood_line(client, END)
        self.assertEqual(client.bar_calls, [(list(FUNDS), END - timedelta(days=HISTORY_DAYS))])
        self.assertEqual(client.bar_options, [{"adjustment": "all"}])

    def test_a_bar_from_after_the_review_day_is_ignored(self):
        bars = fund_bars()
        for fund in FUNDS:                                   # a crash "tomorrow" must not change today's mood
            bars[fund].append(bar(END + timedelta(days=1), 0.01))
        self.assertEqual(fetch_mood_line(FakeClient(bars=bars), END), TEN_OF_FOURTEEN)
        self.assertEqual(len(bars_up_to(bars, END)["SPY"]), 220)

    def test_bars_that_arrive_out_of_order_are_put_in_order(self):
        bars = {fund: list(reversed(rows)) for fund, rows in fund_bars().items()}
        self.assertEqual(fetch_mood_line(FakeClient(bars=bars), END), TEN_OF_FOURTEEN)

    def test_no_line_when_no_fund_has_enough_history(self):
        short = fund_bars(days=LAST_220_DAYS[-50:])
        self.assertEqual(fetch_mood_line(FakeClient(bars=short), END), "")
        self.assertEqual(fetch_mood_line(FakeClient(bars={}), END), "")

    def test_a_problem_with_the_mood_never_raises(self):
        logs = []
        broken = FakeClient(bars=fund_bars(), fail_symbols={"TLT"})
        with self.assertRaises(AlpacaError):
            fetch_mood_line(broken, END)
        self.assertEqual(safe_mood_line(broken, END, log=logs.append), "")
        self.assertIn("market mood skipped", logs[0])
        self.assertEqual(safe_mood_line(FakeClient(bars=fund_bars()), END, log=logs.append), TEN_OF_FOURTEEN)


if __name__ == "__main__":
    unittest.main()
