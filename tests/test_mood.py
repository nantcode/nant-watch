import unittest

from mood import FUNDS, count_uptrends, mood_label, mood_line


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


if __name__ == "__main__":
    unittest.main()
