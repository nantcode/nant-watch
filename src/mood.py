"""Market mood for NantWatch: how many funds are in an uptrend.

Aaron's suggestion (Nov 2026): NantWatch should show what the bots are seeing, starting with
whether the market is bullish or bearish.

NantTrend already asks that question once a month: it holds a fund only while the fund is above
the average of its last 200 closes. Counting how many of its 14 funds pass that test gives one
plain number for the market's mood: 12 of 14 is a strong market, 3 of 14 is a weak one.

Nothing here calls Alpaca or AWS. The functions take plain lists of closing prices, so every rule
can be unit tested with tiny fake inputs (see tests/test_mood.py).
"""
from datetime import date, timedelta
from typing import Callable, Dict, List, Sequence

from market_math import parse_day

# NantTrend's (and NantBot's) 14 funds
FUNDS = ("SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "SMH", "GLD", "TLT")
TREND_DAYS = 200
BULLISH_SHARE = 0.70       # 70% or more of the funds in uptrends: call it bullish
BEARISH_SHARE = 0.30       # 30% or fewer: call it bearish
HISTORY_DAYS = 330         # calendar days of prices to ask Alpaca for (200 trading days need about 290)


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



# ---------- from Alpaca's bars to the line in the post ----------

def closes_from_bars(bars_by_symbol: Dict[str, List[dict]]) -> Dict[str, List[float]]:
    """Alpaca's daily bars -> just the closing prices, fund by fund.

    {"SPY": [{"t": "2026-10-05T04:00:00Z", "c": 670.0, "v": 1}, {"t": "...", "c": 671.5, "v": 1}]}
        ->  {"SPY": [670.0, 671.5]}

    Every fund in the input must appear in the output, with its closes in the same order as its
    bars. A bar's closing price is bar["c"]."""
    closes: Dict[str, List[float]] = {}
    # ---- TASK A: YOUR CODE GOES HERE (about 5 lines) ----
    for symbol, bars in bars_by_symbol.items():
        prices = []
        for bar in bars:
            prices.append(bar["c"])
        closes[symbol] = prices 
    return closes


def bars_up_to(bars_by_symbol: Dict[str, List[dict]], end_day: date) -> Dict[str, List[dict]]:
    """The same bars, oldest first, without anything dated after `end_day`.
    (The 9:30 AM post reviews yesterday, so today's half-finished bar must not be counted.)"""
    return {symbol: [bar for bar in sorted(bars, key=lambda b: b["t"]) if parse_day(bar["t"]) <= end_day]
            for symbol, bars in bars_by_symbol.items()}


def fetch_mood_line(client, end_day: date, funds: Sequence[str] = FUNDS, days: int = TREND_DAYS) -> str:
    """Ask Alpaca for the funds' prices and return the mood line as of `end_day`.
    Returns "" when no fund has enough history to judge.
    adjustment="all" gives the same dividend-adjusted prices NantTrend makes its decisions on."""
    start = end_day - timedelta(days=HISTORY_DAYS)
    bars = client.get_bars(list(funds), start, adjustment="all")
    closes = closes_from_bars(bars_up_to(bars, end_day))
    if not any(len(prices) >= days for prices in closes.values()):
        return ""
    return mood_line(closes, days)


def safe_mood_line(client, end_day: date, log: Callable[[str], None] = print) -> str:
    """The mood line, or "" if anything at all goes wrong.
    The mood is an extra: a problem with it must never stop the rest of the post going out."""
    try:
        return fetch_mood_line(client, end_day)
    except Exception as error:
        log(f"market mood skipped: {type(error).__name__}: {error}")
        return ""
