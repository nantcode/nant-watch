"""M3 smoke test: talk to Alpaca for real, READ-ONLY.

Needs APCA_API_KEY_ID and APCA_API_SECRET_KEY in the environment.
Makes 3 requests: the market calendar, the asset list, and SPY/QQQ bars.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from alpaca_client import AlpacaClient  # noqa: E402
from formatting import fmt_date_label, market_summary  # noqa: E402
from market_math import (  # noqa: E402
    daily_window,
    eligible_symbols,
    movers_from_bars,
    trading_days_from_calendar,
)


def main() -> int:
    key_id = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    if not key_id or not secret:
        print("ERROR: export APCA_API_KEY_ID and APCA_API_SECRET_KEY first (see the M3 guide).", file=sys.stderr)
        return 1
    client = AlpacaClient(key_id, secret)

    today = date.today()
    calendar = trading_days_from_calendar(client.get_calendar(today - timedelta(days=14), today))
    finished = [d for d in calendar if d < today]   # sessions that are completely over
    window = daily_window(calendar, finished[-1])
    print(f"Last full session: {fmt_date_label(window.end_day)} (vs close of {fmt_date_label(window.base_day)})")

    assets = client.get_assets()
    symbols = eligible_symbols(assets)
    print(f"Assets returned: {len(assets):,}  |  eligible to scan: {len(symbols):,}")
    print(f"First few: {', '.join(symbols[:8])}")

    bars = client.get_bars(["SPY", "QQQ"], window.base_day)
    by_symbol = {m.symbol: m for m in movers_from_bars(bars, window, min_price=0, min_avg_volume=0)}
    print(market_summary([by_symbol[s] for s in ("SPY", "QQQ") if s in by_symbol]))
    print(f"Requests used: {client.request_count} (free plan limit: 200/min)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
