"""Market scanner: joins the Alpaca client, the market math and the
formatter into finished reports.

The client is passed in (dependency injection), so the tests hand it a
FakeClient and check the whole pipeline without touching the network.
"""
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Callable, List, Optional, Sequence, Tuple

from alpaca_client import AlpacaError, chunked
from formatting import Mover, build_daily_report, fmt_date_label
from market_math import (
    Window,
    daily_window,
    eligible_symbols,
    movers_from_bars,
    rank_movers,
    trading_days_from_calendar,
)

INDEX_SYMBOLS = ("SPY", "QQQ")
TOP_N = 10
BATCH_SIZE = 200
SESSION_DONE = clock_time(16, 30)   # daily bars are settled a bit after the 4:00 PM close

ProgressFn = Callable[[int, int], None]


@dataclass
class ScanResult:
    window: Window
    indexes: List[Mover]
    gainers: List[Mover]
    losers: List[Mover]
    symbols_scanned: int
    symbols_kept: int
    failed_batches: int
    requests_used: int
    seconds: float

    def summary(self) -> str:
        """One line for logs: what was scanned and what it cost."""
        return (
            f"window {self.window.base_day} -> {self.window.end_day} | "
            f"scanned {self.symbols_scanned:,} | passed filters {self.symbols_kept:,} | "
            f"failed batches {self.failed_batches} | requests {self.requests_used} | "
            f"{self.seconds:.1f}s"
        )


def pick_report_day(trading_days: Sequence[date], now: datetime) -> Optional[date]:
    """For manual runs: the latest session that is completely finished.
    Today only counts if it's a trading day and it's past 4:30 PM."""
    today = now.date()
    if today in trading_days and now.time() >= SESSION_DONE:
        return today
    earlier = [d for d in trading_days if d < today]
    return max(earlier) if earlier else None


def load_trading_days(client, around: date, days_back: int = 21) -> List[date]:
    """About three weeks of market calendar ending on `around`."""
    return trading_days_from_calendar(client.get_calendar(around - timedelta(days=days_back), around))


def scan_market(
    client,
    window: Window,
    symbols: Sequence[str],
    batch_size: int = BATCH_SIZE,
    on_batch: Optional[ProgressFn] = None,
) -> Tuple[List[Mover], int]:
    """Fetch bars for every symbol in batches; keep the ones that pass the filters.
    One bad batch is counted and skipped so it can't sink the whole report,
    but if EVERY batch fails (bad keys, Alpaca down) we raise."""
    movers: List[Mover] = []
    batches = chunked(list(symbols), batch_size)
    failed = 0
    last_error: Optional[AlpacaError] = None
    for number, batch in enumerate(batches, start=1):
        try:
            bars = client.get_bars(batch, window.base_day)
            movers.extend(movers_from_bars(bars, window))
        except AlpacaError as err:
            failed += 1
            last_error = err
        if on_batch:
            on_batch(number, len(batches))
    if batches and failed == len(batches):
        raise AlpacaError(f"every batch failed; last error: {last_error}")
    return movers, failed


def index_movers(client, window: Window, symbols: Sequence[str] = INDEX_SYMBOLS) -> List[Mover]:
    """SPY/QQQ for the market summary (no price/volume filters), in a fixed order."""
    bars = client.get_bars(list(symbols), window.base_day)
    found = {m.symbol: m for m in movers_from_bars(bars, window, min_price=0, min_avg_volume=0)}
    return [found[s] for s in symbols if s in found]


def run_scan(
    client,
    window: Window,
    top_n: int = TOP_N,
    on_batch: Optional[ProgressFn] = None,
    clock: Callable[[], float] = time.monotonic,
    batch_size: int = BATCH_SIZE,
) -> ScanResult:
    """Assets -> eligible symbols -> batched bars -> filters -> top N, plus indexes."""
    started = clock()
    symbols = eligible_symbols(client.get_assets())
    movers, failed = scan_market(client, window, symbols, batch_size, on_batch)
    gainers, losers = rank_movers(movers, top_n)
    indexes = index_movers(client, window)
    return ScanResult(
        window=window,
        indexes=indexes,
        gainers=gainers,
        losers=losers,
        symbols_scanned=len(symbols),
        symbols_kept=len(movers),
        failed_batches=failed,
        requests_used=client.request_count,
        seconds=clock() - started,
    )


def build_daily(
    client,
    report_day: date,
    top_n: int = TOP_N,
    on_batch: Optional[ProgressFn] = None,
) -> Optional[Tuple[ScanResult, List[str]]]:
    """Everything the daily job does except posting.
    Returns None when report_day isn't a trading day (weekend/holiday)."""
    window = daily_window(load_trading_days(client, report_day), report_day)
    if window is None:
        return None
    result = run_scan(client, window, top_n, on_batch)
    messages = build_daily_report(
        fmt_date_label(window.end_day), result.indexes, result.gainers, result.losers
    )
    return result, messages
