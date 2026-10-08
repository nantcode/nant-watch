"""Pure market math for NantWatch: trading-day windows, stock filters,
percent change and ranking.

Nothing here calls Alpaca or AWS. Functions take plain Python data
(the same shapes Alpaca returns as JSON) and return plain data, so every
rule can be unit tested with tiny fake inputs.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from formatting import Mover

MIN_PRICE = 5.0            # skip penny stocks (checked at the start AND end of the window)
MIN_AVG_VOLUME = 500_000   # skip thin stocks (average shares per day in the window)
BLOCKED_EXCHANGES = {"OTC", "CRYPTO", ""}

# Warrants, units and rights are not regular shares. They swing wildly and
# would crowd real companies out of the top-10 lists. \b = "word boundary",
# so "United Airlines" is NOT matched by "unit".
_NOT_COMMON_STOCK = re.compile(r"\b(warrants?|units?|rights?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Bar:
    """One daily bar: the trading day, its closing price and shares traded."""
    day: date
    close: float
    volume: float


@dataclass(frozen=True)
class Window:
    """The period a report covers.

    base_day: the close we measure FROM (the last session before the period)
    days:     the trading sessions inside the period, oldest first
    """
    base_day: date
    days: Tuple[date, ...]

    @property
    def end_day(self) -> date:
        return self.days[-1]


# ---------- reading Alpaca JSON ----------

def parse_day(text: str) -> date:
    """'2026-10-05' or '2026-10-05T04:00:00Z' -> date(2026, 10, 5).
    Alpaca stamps daily bars at 04:00 UTC = midnight New York, so the
    first 10 characters are always the correct market date."""
    return date.fromisoformat(text[:10])


def bar_from_alpaca(raw: dict) -> Bar:
    """{'t': '2026-10-05T04:00:00Z', 'c': 12.3, 'v': 1000, ...} -> Bar."""
    return Bar(day=parse_day(raw["t"]), close=float(raw["c"]), volume=float(raw["v"]))


def trading_days_from_calendar(raw_calendar: Iterable[dict]) -> List[date]:
    """Alpaca /v2/calendar JSON [{'date': '2026-10-05', ...}, ...] -> sorted, unique dates."""
    return sorted({parse_day(entry["date"]) for entry in raw_calendar})


# ---------- which days does a report cover? ----------

def daily_window(trading_days: Sequence[date], today: date) -> Optional[Window]:
    """Today's session measured from the previous session's close.
    Returns None when today is not a trading day (weekend or holiday),
    which tells the daily job to skip posting."""
    days = sorted(trading_days)
    if today not in days:
        return None
    index = days.index(today)
    if index == 0:
        return None  # calendar doesn't reach back far enough to find a base day
    return Window(base_day=days[index - 1], days=(today,))


def weekly_window(trading_days: Sequence[date], today: date) -> Optional[Window]:
    """The most recent trading week on or before `today` (we run it Saturday).
    Measured from the close of the last session BEFORE that week, so a
    Monday or Friday holiday is handled automatically."""
    days = sorted(d for d in trading_days if d <= today)
    if not days:
        return None
    end = days[-1]
    week_start = end - timedelta(days=end.weekday())   # Monday of that week
    week = tuple(d for d in days if d >= week_start)
    earlier = [d for d in days if d < week_start]
    if not earlier:
        return None
    return Window(base_day=earlier[-1], days=week)


# ---------- which stocks count? ----------

def is_eligible_asset(asset: dict) -> bool:
    """Keep active, tradable US stocks/ETFs listed on real exchanges."""
    if asset.get("class") != "us_equity":
        return False
    if asset.get("status") != "active" or not asset.get("tradable", False):
        return False
    if asset.get("exchange", "") in BLOCKED_EXCHANGES:
        return False
    if _NOT_COMMON_STOCK.search(asset.get("name") or ""):
        return False
    return True


def eligible_symbols(assets: Iterable[dict]) -> List[str]:
    """Alpaca /v2/assets JSON -> sorted list of symbols worth scanning."""
    return sorted({a["symbol"] for a in assets if is_eligible_asset(a)})


# ---------- the math ----------

def pct_change(start: float, end: float) -> float:
    """pct_change(100, 103) -> 3.0 (percent)."""
    if start <= 0:
        raise ValueError("start price must be positive")
    return (end - start) / start * 100.0


def compute_mover(
    symbol: str,
    bars: Sequence[Bar],
    window: Window,
    min_price: float = MIN_PRICE,
    min_avg_volume: float = MIN_AVG_VOLUME,
) -> Optional[Mover]:
    """Turn one symbol's daily bars into a Mover for `window`,
    or None if it should be skipped (missing data, too cheap, too thin)."""
    by_day: Dict[date, Bar] = {b.day: b for b in bars}
    base = by_day.get(window.base_day)
    end = by_day.get(window.end_day)
    if base is None or end is None:
        return None  # no bar on a day we need -> an old price would give a fake % move
    if base.close <= 0:
        return None
    if base.close < min_price or end.close < min_price:
        return None
    # Days with no bar count as zero volume, so on-and-off traders score low.
    total_volume = sum(by_day[d].volume for d in window.days if d in by_day)
    avg_volume = total_volume / len(window.days)
    if avg_volume < min_avg_volume:
        return None
    return Mover(symbol, end.close, pct_change(base.close, end.close), avg_volume)


def movers_from_bars(
    bars_by_symbol: Dict[str, List[dict]],
    window: Window,
    min_price: float = MIN_PRICE,
    min_avg_volume: float = MIN_AVG_VOLUME,
) -> List[Mover]:
    """Alpaca multi-bars JSON {'AAPL': [bar, bar, ...], ...} -> list of Movers
    (symbols that fail a filter are simply left out)."""
    movers: List[Mover] = []
    for symbol, raw_bars in bars_by_symbol.items():
        bars = [bar_from_alpaca(raw) for raw in raw_bars]
        mover = compute_mover(symbol, bars, window, min_price, min_avg_volume)
        if mover is not None:
            movers.append(mover)
    return movers


def rank_movers(movers: Iterable[Mover], top_n: int = 10) -> Tuple[List[Mover], List[Mover]]:
    """Split into (top gainers, top losers), biggest moves first.
    Ties are broken alphabetically so the same data always gives the same list."""
    movers = list(movers)
    gainers = sorted((m for m in movers if m.change_pct > 0), key=lambda m: (-m.change_pct, m.symbol))
    losers = sorted((m for m in movers if m.change_pct < 0), key=lambda m: (m.change_pct, m.symbol))
    return gainers[:top_n], losers[:top_n]
