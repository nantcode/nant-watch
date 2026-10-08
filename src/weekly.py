"""Weekly breakdown: the week's top movers across the market + the race scoreboard.

Reuses everything we already built: the same scanner (just a 5-day window
instead of 1 day), the same formatter pieces, and the M6 scoreboard.
"""
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from formatting import BOT_NAME, Mover, market_summary, movers_table, pack_messages, section
from market_math import Window, trading_days_from_calendar, weekly_window
from race import RACE_START, Racer, build_scoreboard
from scanner import TOP_N, ProgressFn, ScanResult, pick_report_day, run_scan


def week_label(window: Window) -> str:
    """Window over Oct 5-9 -> 'Mon Oct 5 – Fri Oct 9, 2026'."""
    first, last = window.days[0], window.end_day
    return f"{first:%a %b} {first.day} – {last:%a %b} {last.day}, {last.year}"


def build_weekly_report(label: str, indexes: Sequence[Mover], gainers: Sequence[Mover],
                        losers: Sequence[Mover], scoreboard_text: str) -> List[str]:
    """The Saturday post, as a list of Discord-safe messages."""
    sections = [
        f"📅 **{BOT_NAME} · Weekly Breakdown** · {label}",
        section("Market (week)", market_summary(indexes)),
        section("🚀 Top Weekly Gainers", movers_table(gainers)),
        section("📉 Top Weekly Losers", movers_table(losers)),
        scoreboard_text,
        "_Read-only market recap. VOL = average daily volume. Not financial advice._",
    ]
    return pack_messages(sections)


def build_weekly(
    client,
    accounts: Dict[str, object],
    now: datetime,
    as_of: Optional[date] = None,
    top_n: int = TOP_N,
    on_batch: Optional[ProgressFn] = None,
) -> Optional[Tuple[ScanResult, List[str], List[Racer]]]:
    """Everything the weekly job does except posting.

    as_of=None means "the latest finished session" (on Saturday that's Friday).
    One calendar request covers both the week and the race start."""
    calendar_start = min(RACE_START, now.date()) - timedelta(days=21)
    trading_days = trading_days_from_calendar(client.get_calendar(calendar_start, now.date()))
    report_day = as_of or pick_report_day(trading_days, now)
    if report_day is None:
        return None
    window = weekly_window(trading_days, report_day)
    if window is None:
        return None
    result = run_scan(client, window, top_n, on_batch)
    scoreboard_text, racers = build_scoreboard(accounts, client, trading_days)
    messages = build_weekly_report(week_label(window), result.indexes, result.gainers,
                                   result.losers, scoreboard_text)
    return result, messages, racers
