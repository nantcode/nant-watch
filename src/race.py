"""Race scoreboard: every bot in racers.json vs SPY since the race started.

Everyone is measured the same way: from the CLOSE of the last session
before the race (Fri Oct 2, 2026) to the latest close we have.
Results are also shown as "what $10K at the start is worth now", so a
$100k bot account and a $700 ETF can be compared fairly.
"""
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from alpaca_client import AlpacaError
from formatting import CODE_FENCE, fmt_date_label, fmt_pct, fmt_price, section
from market_math import bar_from_alpaca, pct_change

RACE_START = date(2026, 10, 5)
BENCHMARK = "SPY"
STAKE = 10_000.0
MEDALS = ("🥇", "🥈", "🥉")
RACERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "racers.json")
NAME_WIDTH = 10            # the RACER column on the scoreboard
SSM_PREFIX = "/nant-watch/racers"


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


# ---------- who's racing: racers.json ----------

@dataclass(frozen=True)
class Entrant:
    """One bot entered in the race. The main entrant is the account whose keys
    also read market data (NantBot); everyone else brings their own read-only keys."""
    name: str
    main: bool = False
    key_id_param: str = ""
    secret_key_param: str = ""
    key_id_env: str = ""
    secret_key_env: str = ""

    @property
    def slug(self) -> str:
        return slug_for(self.name)

    @property
    def key_id_secret(self) -> str:
        """Key in the secrets dict, e.g. 'sparticus_key_id'."""
        return f"{self.slug}_key_id"

    @property
    def secret_key_secret(self) -> str:
        return f"{self.slug}_secret_key"


def slug_for(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_entrants(config: dict) -> List[Entrant]:
    """racers.json contents -> entrants, failing loudly on anything that would
    break the scoreboard or mix up two accounts."""
    raw = config.get("racers") if isinstance(config, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("racers config needs a non-empty 'racers' list")
    known = {"name", "main", "key_id_param", "secret_key_param", "key_id_env", "secret_key_env"}
    entrants: List[Entrant] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"racer #{i + 1} must be an object")
        unknown = sorted(set(item) - known)
        if unknown:
            raise ValueError(f"racer #{i + 1} has unknown field(s): {', '.join(unknown)}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"racer #{i + 1} needs a name")
        name = name.strip()
        if len(name) > NAME_WIDTH:
            raise ValueError(f"racer name {name!r} is longer than {NAME_WIDTH} characters")
        if not slug_for(name):
            raise ValueError(f"racer name {name!r} needs at least one letter or digit")
        if name.upper() == BENCHMARK:
            raise ValueError(f"{BENCHMARK} is the benchmark and can't be entered as a racer")
        main = item.get("main", False)
        if not isinstance(main, bool):
            raise ValueError(f"racer {name!r}: 'main' must be true or false")
        for field in known - {"name", "main"}:
            if field in item and (not isinstance(item[field], str) or not item[field].strip()):
                raise ValueError(f"racer {name!r}: {field!r} must be a non-empty string")
        slug = slug_for(name)
        entrants.append(Entrant(
            name=name,
            main=main,
            key_id_param=item.get("key_id_param", f"{SSM_PREFIX}/{slug}/key_id"),
            secret_key_param=item.get("secret_key_param", f"{SSM_PREFIX}/{slug}/secret_key"),
            key_id_env=item.get("key_id_env", f"{slug.upper()}_KEY_ID"),
            secret_key_env=item.get("secret_key_env", f"{slug.upper()}_SECRET_KEY"),
        ))
    slugs = [e.slug for e in entrants]
    dupes = sorted({e.name for e in entrants if slugs.count(e.slug) > 1})
    if dupes:
        raise ValueError(f"racer names must be unique: {', '.join(dupes)}")
    if sum(e.main for e in entrants) > 1:
        raise ValueError("only one racer can be the main account")
    return entrants


def load_entrants(path: str = RACERS_FILE) -> List[Entrant]:
    with open(path, encoding="utf-8") as handle:
        try:
            config = json.load(handle)
        except json.JSONDecodeError as error:
            raise ValueError(f"{os.path.basename(path)} is not valid JSON: {error}") from None
    return parse_entrants(config)


def make_race_accounts(entrants: Sequence[Entrant], main_client, main_key_id: str,
                       keys: Dict[str, Tuple[str, str]],
                       make_client: Callable[[str, str], object],
                       log: Callable[[str], None]) -> Dict[str, object]:
    """Entrants -> {name: read-only client, or None if we have no keys for it}.
    keys maps an entrant's name to its (key_id, secret_key)."""
    accounts: Dict[str, object] = {}
    owner_of_key: Dict[str, str] = {}
    for entrant in entrants:
        if entrant.main:
            accounts[entrant.name] = main_client
            owner_of_key.setdefault(main_key_id, entrant.name)
            continue
        key_id, secret_key = keys.get(entrant.name, ("", ""))
        if not key_id or not secret_key:
            log(f"no {entrant.name} keys found, it shows as 'no data yet'")
            accounts[entrant.name] = None
            continue
        if key_id == main_key_id or key_id in owner_of_key:
            other = owner_of_key.get(key_id, "the main account")
            log(f"WARNING: {entrant.name} and {other} share one Alpaca account; their results will match")
        owner_of_key.setdefault(key_id, entrant.name)
        accounts[entrant.name] = make_client(key_id, secret_key)
    return accounts


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

def account_series(client, intraday: bool = False) -> List[Tuple[date, float]]:
    """Daily closing values for the whole race. With intraday=True, today's
    latest 5-minute value is laid on top, so the scoreboard is live."""
    by_day = dict(equity_series(client.get_portfolio_history()))
    if intraday:
        by_day.update(equity_series(client.get_portfolio_history(period="1D", timeframe="5Min")))
    return sorted(by_day.items())


def collect_racers(accounts: Dict[str, object], market_client, trading_days: Sequence[date],
                   race_start: date = RACE_START,
                   intraday: bool = False) -> Tuple[List[Racer], List[str], date]:
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
                racer = racer_from_series(name, account_series(client, intraday), base_day)
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
                     race_start: date = RACE_START, intraday: bool = False,
                     as_of_label: Optional[str] = None) -> Tuple[str, List[Racer]]:
    """Everything in one call: (Discord section text, racers for logging)."""
    racers, missing, _ = collect_racers(accounts, market_client, trading_days, race_start, intraday)
    as_of = max((r.end_day for r in racers), default=race_start)
    text = format_scoreboard(racers, fmt_date_label(race_start),
                             as_of_label or fmt_date_label(as_of), missing)
    return text, racers
