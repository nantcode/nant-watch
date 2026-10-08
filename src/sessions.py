"""Weekday posts (v1.1): market-open review, live race updates, market-close review.

  open  (9:30 AM)          full review of the LAST session + live race board
  race  (11 AM, 12, 2 PM)  live race board only
  close (4:00 PM)          full review of TODAY + live race board
"""
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from formatting import (
    BOT_NAME,
    Mover,
    fmt_date_label,
    fmt_pct,
    market_summary,
    movers_table,
    pack_messages,
    section,
)
from market_math import daily_window, trading_days_from_calendar
from race import RACE_START, Racer, build_scoreboard
from scanner import run_scan

MODES = ("open", "race", "close")
DATA_DELAY = timedelta(minutes=15)   # Alpaca's free plan serves SIP prices 15 minutes late


def clock_label(moment: datetime) -> str:
    """datetime(..., 11, 0) -> '11:00 AM', datetime(..., 9, 30) -> '9:30 AM'."""
    return f"{moment:%I:%M %p}".lstrip("0")


def live_label(now_et: datetime) -> str:
    """-> 'Thu Oct 8, 11:00 AM ET'."""
    return f"{now_et:%a %b} {now_et.day}, {clock_label(now_et)} ET"


def load_session_calendar(client, today: date) -> List[date]:
    """One calendar call that reaches back past the race start."""
    start = min(RACE_START, today) - timedelta(days=21)
    return trading_days_from_calendar(client.get_calendar(start, today))


def race_summary(racers: Sequence[Racer]) -> str:
    return "race: " + " | ".join(f"{r.name} {fmt_pct(r.return_pct)}" for r in racers)


def build_session_report(header: str, indexes: Sequence[Mover], gainers: Sequence[Mover],
                         losers: Sequence[Mover], scoreboard_text: str, footer: str) -> List[str]:
    sections = [
        header,
        section("Market", market_summary(indexes)),
        section("🚀 Top Gainers", movers_table(gainers)),
        section("📉 Top Losers", movers_table(losers)),
        scoreboard_text,
        footer,
    ]
    return pack_messages(sections)


def race_post(client, accounts: Dict[str, object], trading_days: Sequence[date],
              now_et: datetime) -> Tuple[List[str], str]:
    """11 AM / 12 PM / 2 PM: just the live race board."""
    text, racers = build_scoreboard(accounts, client, trading_days, intraday=True,
                                    as_of_label=live_label(now_et))
    footer = "_Bots: account value right now · SPY: price from about 15 minutes ago (free data)._"
    return pack_messages([text, footer]), race_summary(racers)


def review_post(mode: str, client, accounts: Dict[str, object], trading_days: Sequence[date],
                now_et: datetime, session_day: Optional[date] = None) -> Tuple[Optional[List[str]], str]:
    """9:30 AM (mode='open') reviews the last finished session.
    4:00 PM (mode='close') reviews today, with prices about 15 minutes old."""
    if session_day is None:
        if mode == "open":
            earlier = [d for d in trading_days if d < now_et.date()]
            session_day = max(earlier) if earlier else None
        else:
            session_day = now_et.date()
    window = daily_window(trading_days, session_day) if session_day else None
    if window is None:
        return None, f"{session_day} is not a trading day, nothing to review"

    result = run_scan(client, window)
    if result.symbols_kept == 0:
        raise RuntimeError(f"no usable market data for {window.end_day} yet ({result.summary()})")
    scoreboard_text, racers = build_scoreboard(accounts, client, trading_days, intraday=True,
                                               as_of_label=live_label(now_et))
    day_label = fmt_date_label(window.end_day)
    if mode == "open":
        header = f"🔔 **{BOT_NAME} · Market Open Review** · recap of {day_label}"
        footer = "_Movers are from the last full session. Read-only recap, not financial advice._"
    else:
        header = f"🔔 **{BOT_NAME} · Market Close Review** · {day_label}"
        if window.end_day < now_et.date():
            footer = "_Final closing prices. Read-only recap, not financial advice._"
        else:
            as_of = clock_label(now_et - DATA_DELAY)
            footer = (f"_Prices as of about {as_of} ET (free data runs 15 minutes behind). "
                      "Not financial advice._")
    messages = build_session_report(header, result.indexes, result.gainers, result.losers,
                                    scoreboard_text, footer)
    return messages, f"{result.summary()} | {race_summary(racers)}"


def build_session_post(mode: str, client, accounts: Dict[str, object], trading_days: Sequence[date],
                       now_et: datetime, session_day: Optional[date] = None) -> Tuple[Optional[List[str]], str]:
    """Pick the right post for the mode. Returns (messages or None to skip, log line)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode} (use one of {', '.join(MODES)})")
    if mode == "race":
        return race_post(client, accounts, trading_days, now_et)
    return review_post(mode, client, accounts, trading_days, now_et, session_day)
