import unittest
from datetime import date

from formatting import (
    CODE_FENCE,
    DISCORD_HARD_LIMIT,
    SAFE_LIMIT,
    Mover,
    build_daily_report,
    fmt_date_label,
    fmt_pct,
    fmt_price,
    fmt_volume,
    market_summary,
    movers_table,
    pack_messages,
    trend_emoji,
    truncate,
)


def make_movers(count, start_pct=10.0):
    return [
        Mover(f"T{i:03d}", 10.0 + i, start_pct - i, 1_000_000 + i)
        for i in range(count)
    ]


class TestValueFormatters(unittest.TestCase):
    def test_fmt_pct_signs(self):
        self.assertEqual(fmt_pct(3.14159), "+3.14%")
        self.assertEqual(fmt_pct(-0.5), "-0.50%")
        self.assertEqual(fmt_pct(0), "0.00%")

    def test_fmt_pct_never_negative_zero(self):
        self.assertEqual(fmt_pct(-0.004), "0.00%")

    def test_fmt_pct_decimals(self):
        self.assertEqual(fmt_pct(12.345, 1), "+12.3%")

    def test_fmt_price(self):
        self.assertEqual(fmt_price(1234.5), "$1,234.50")
        self.assertEqual(fmt_price(5), "$5.00")

    def test_fmt_volume(self):
        self.assertEqual(fmt_volume(950), "950")
        self.assertEqual(fmt_volume(12_300), "12.3K")
        self.assertEqual(fmt_volume(4_100_000), "4.1M")
        self.assertEqual(fmt_volume(2_500_000_000), "2.5B")

    def test_fmt_volume_rolls_up_at_boundary(self):
        self.assertEqual(fmt_volume(999_999), "1.0M")

    def test_fmt_date_label(self):
        self.assertEqual(fmt_date_label(date(2026, 10, 6)), "Tue Oct 6, 2026")

    def test_trend_emoji(self):
        self.assertEqual(trend_emoji(1.0), "🟢")
        self.assertEqual(trend_emoji(-1.0), "🔴")
        self.assertEqual(trend_emoji(0.0), "⚪")


class TestBuildingBlocks(unittest.TestCase):
    def test_market_summary_lines(self):
        text = market_summary([Mover("SPY", 672.41, 0.84, 1), Mover("QQQ", 598.1, -0.37, 1)])
        self.assertEqual(
            text.splitlines(),
            ["🟢 **SPY** $672.41 (+0.84%)", "🔴 **QQQ** $598.10 (-0.37%)"],
        )

    def test_market_summary_empty(self):
        self.assertEqual(market_summary([]), "(no data)")

    def test_movers_table_shape(self):
        table = movers_table(make_movers(3))
        lines = table.splitlines()
        self.assertEqual(lines[0], CODE_FENCE)
        self.assertEqual(lines[-1], CODE_FENCE)
        self.assertEqual(len(lines), 2 + 1 + 3)  # fences + header + 3 rows
        self.assertIn("T000", lines[2])
        self.assertTrue(lines[2].lstrip().startswith("1 "))

    def test_movers_table_empty(self):
        self.assertEqual(movers_table([]), "(no data)")


class TestDiscordSizeSafety(unittest.TestCase):
    def test_truncate_leaves_short_text_alone(self):
        self.assertEqual(truncate("hello", 100), "hello")

    def test_truncate_respects_limit_and_closes_code_block(self):
        text = CODE_FENCE + "\n" + "\n".join("row %d" % i for i in range(1000)) + "\n" + CODE_FENCE
        result = truncate(text, 200)
        self.assertLessEqual(len(result), 200)
        self.assertEqual(result.count(CODE_FENCE) % 2, 0)  # fences balanced
        self.assertTrue(result.endswith("(truncated)"))

    def test_pack_combines_small_sections(self):
        self.assertEqual(pack_messages(["a", "b", "c"], limit=100), ["a\n\nb\n\nc"])

    def test_pack_splits_when_full_and_keeps_order(self):
        sections = ["x" * 60, "y" * 60, "z" * 60]
        messages = pack_messages(sections, limit=100)
        self.assertEqual(messages, ["x" * 60, "y" * 60, "z" * 60])

    def test_pack_skips_empty_and_truncates_huge(self):
        messages = pack_messages(["", "   ", "w" * 500], limit=100)
        self.assertEqual(len(messages), 1)
        self.assertLessEqual(len(messages[0]), 100)


class TestDailyReport(unittest.TestCase):
    def setUp(self):
        self.indexes = [Mover("SPY", 672.41, 0.84, 61_200_000), Mover("QQQ", 598.10, -0.37, 38_900_000)]

    def test_normal_report_is_one_message(self):
        messages = build_daily_report("Tue Oct 6, 2026", self.indexes, make_movers(5), make_movers(5, -1))
        self.assertEqual(len(messages), 1)
        self.assertIn("Tue Oct 6, 2026", messages[0])
        self.assertIn("**SPY**", messages[0])
        self.assertIn("Top Gainers", messages[0])
        self.assertIn("Top Losers", messages[0])

    def test_huge_report_splits_and_every_message_fits(self):
        messages = build_daily_report("Tue Oct 6, 2026", self.indexes, make_movers(40), make_movers(40, -1))
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), SAFE_LIMIT)
            self.assertLess(len(message), DISCORD_HARD_LIMIT)
            self.assertEqual(message.count(CODE_FENCE) % 2, 0)


if __name__ == "__main__":
    unittest.main()
