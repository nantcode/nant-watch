"""Market mood for NantWatch: how many funds are in an uptrend.

Aaron's suggestion (Nov 2026): NantWatch should show what the bots are seeing, starting with
whether the market is bullish or bearish.

NantTrend already asks that question once a month: it holds a fund only while the fund is above
the average of its last 200 closes. Counting how many of its 14 funds pass that test gives one
plain number for the market's mood: 12 of 14 is a strong market, 3 of 14 is a weak one.

Nothing here calls Alpaca or AWS. The functions take plain lists of closing prices, so every rule
can be unit tested with tiny fake inputs (see tests/test_mood.py).
"""
from typing import Dict, Sequence

# NantTrend's (and NantBot's) 14 funds
FUNDS = ("SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "SMH", "GLD", "TLT")
TREND_DAYS = 200
BULLISH_SHARE = 0.70       # 70% or more of the funds in uptrends: call it bullish
BEARISH_SHARE = 0.30       # 30% or fewer: call it bearish


def count_uptrends(closes_by_symbol: Dict[str, Sequence[float]], days: int = TREND_DAYS) -> int:
    """How many funds closed above the average of their last `days` closes.

    closes_by_symbol: {"SPY": [oldest close, ..., latest close], "QQQ": [...], ...}
    A fund with fewer than `days` closes cannot be judged, so it is not counted."""
    count = 0
    for closes in closes_by_symbol.values():
        if len(closes) < days:
            continue                                  # not enough history: skip this fund
        average = sum(closes[-days:]) / days          # the average of its last `days` closes
        if closes[-1] > average:
            count += 1
    return count


def mood_label(up: int, total: int) -> str:
    """'bullish', 'mixed' or 'bearish' from how many funds are in uptrends.
    This is only a word for the post. It is not a trading rule."""
    if total <= 0:
        return "unknown"
    share = up / total
    if share >= BULLISH_SHARE:
        return "bullish"
    if share <= BEARISH_SHARE:
        return "bearish"
    return "mixed"


def mood_line(closes_by_symbol: Dict[str, Sequence[float]], days: int = TREND_DAYS) -> str:
    """One line for a NantWatch post, for example:
    'Market mood: 10 of 14 funds above their 200-day average (bullish)'"""
    judged = sum(1 for closes in closes_by_symbol.values() if len(closes) >= days)
    up = count_uptrends(closes_by_symbol, days)
    return f"Market mood: {up} of {judged} funds above their {days}-day average ({mood_label(up, judged)})"
