"""Race scoreboard: NantBot vs Sparticus vs SPY since the race started.

Everyone is measured the same way: from the CLOSE of the last session
before the race (Fri Oct 2, 2026) to the latest close we have.
Results are also shown as "what $10K at the start is worth now", so a
$100k bot account and a $700 ETF can be compared fairly.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from alpaca_client import AlpacaError
from formatting import CODE_FENCE, fmt_date_label, fmt_pct, fmt_price, section
from market_math import bar_from_alpaca, pct_change

RACE_START = date(2026, 10, 5)
BENCHMARK = "SPY"
STAKE = 10_000.0
MEDALS = ("🥇", "🥈", "🥉")


@dataclass(frozen=True)
class Racer:
    name: str
    start_value: float
    end_value: float
    end_day: date

    @property
    def return_pct(self) -> float:
        return pct_change(self.start_value, self.end_value)

    @property
    def stake_now(self) -> float:
        """What $10,000 invested at the start would be worth now."""
        return STAKE * self.end_value / self.start_value


# ---------- turning Alpaca data into (day, value) series ----------

def race_base_day(trading_days: Sequence[date], race_start: date = RACE_START) -> Optional[date]:
    """The last session BEFORE the race: its close is everyone's starting line."""
    earlier = [d for d in trading_days if d < race_start]
    return max(earlier) if earlier else None


def equity_series(history: dict) -> List[Tuple[date, float]]:
    """Alpaca portfolio history JSON {'timestamp': [...], 'equity': [...]}
    -> [(day, equity), ...] oldest first. Empty days (None or 0, e.g. before
    the account existed) are dropped."""
    by_day: Dict[date, float] = {}
    for stamp, equity in zip(history.get("timestamp") or [], history.get("equity") or []):
        if equity is None or equity <= 0:
            continue
        by_day[datetime.fromtimestamp(stamp, tz=timezone.utc).date()] = float(equity)
    return sorted(by_day.items())


def close_series(raw_bars: Sequence[dict]) -> List[Tuple[date, float]]:
    """Alpaca bars -> [(day, close), ...] oldest first."""
    return sorted((bar.day, bar.close) for bar in map(bar_from_alpaca, raw_bars) if bar.close > 0)


def racer_from_series(name: str, series: Sequence[Tuple[date, float]], base_day: date) -> Optional[Racer]:
    """Start = value at the base day's close (or the first value we have, for
    an account that didn't exist yet). End = the latest value."""
    if not series:
        return None
    before = [value for day, value in series if day <= base_day]
    start = before[-1] if before else series[0][1]
    end_day, end = series[-1]
    return Racer(name, start, end, end_day)


def rank_racers(racers: Sequence[Racer]) -> List[Racer]:
    """Best return first; ties broken by name so the order never flickers."""
    return sorted(racers, key=lambda r: (-r.return_pct, r.name))


# ---------- the Discord section ----------

def format_scoreboard(racers: Sequence[Racer], start_label: str, as_of_label: str,
                      missing: Sequence[str] = ()) -> str:
    title = f"🏁 Race Scoreboard · since {start_label} · as of {as_of_label}"
    if not racers:
        return section(title, "(no data yet)")
    ranked = rank_racers(racers)
    lines = [f"   {'RACER':<10} {'RETURN':>8} {'$10K NOW':>11}"]
    for place, racer in enumerate(ranked):
        badge = MEDALS[place] if place < len(MEDALS) else f"{place + 1:>2}"
        lines.append(f"{badge} {racer.name:<10} {fmt_pct(racer.return_pct):>8} {fmt_price(racer.stake_now):>11}")
    notes = []
    benchmark = next((r for r in racers if r.name == BENCHMARK), None)
    if benchmark is not None:
        for racer in ranked:
            if racer.name == BENCHMARK:
                continue
            gap = racer.return_pct - benchmark.return_pct
            if round(gap, 2) == 0:
                notes.append(f"{racer.name} is tied with {BENCHMARK}")
            else:
                verdict = "beating" if gap > 0 else "trailing"
                notes.append(f"{racer.name} is {verdict} {BENCHMARK} by {abs(gap):.2f} pts")
    notes.extend(f"{name}: no data yet" for name in missing)
    body = CODE_FENCE + "\n" + "\n".join(lines) + "\n" + CODE_FENCE
    if notes:
        body += "\n" + "\n".join(notes)
    return section(title, body)


# ---------- gathering the data ----------

def collect_racers(accounts: Dict[str, object], market_client, trading_days: Sequence[date],
                   race_start: date = RACE_START) -> Tuple[List[Racer], List[str], date]:
    """accounts maps a bot name to its own read-only AlpacaClient (or None if we
    have no keys for it). Returns (racers, names with no data, base_day)."""
    base_day = race_base_day(trading_days, race_start)
    if base_day is None:
        raise ValueError("market calendar doesn't reach back before the race start")
    racers: List[Racer] = []
    missing: List[str] = []
    for name, client in accounts.items():
        racer = None
        if client is not None:
            try:
                racer = racer_from_series(name, equity_series(client.get_portfolio_history()), base_day)
            except AlpacaError:
                racer = None
        if racer is None:
            missing.append(name)
        else:
            racers.append(racer)
    bars = market_client.get_bars([BENCHMARK], base_day).get(BENCHMARK, [])
    benchmark = racer_from_series(BENCHMARK, close_series(bars), base_day)
    if benchmark is None:
        missing.append(BENCHMARK)
    else:
        racers.append(benchmark)
    return racers, missing, base_day


def build_scoreboard(accounts: Dict[str, object], market_client, trading_days: Sequence[date],
                     race_start: date = RACE_START) -> Tuple[str, List[Racer]]:
    """Everything in one call: (Discord section text, racers for logging)."""
    racers, missing, _ = collect_racers(accounts, market_client, trading_days, race_start)
    as_of = max((r.end_day for r in racers), default=race_start)
    text = format_scoreboard(racers, fmt_date_label(race_start), fmt_date_label(as_of), missing)
    return text, racers
