"""M4: run the full daily scan from your Mac (read-only).

    python3 scripts/run_daily_local.py                     # scan + print, no posting
    python3 scripts/run_daily_local.py --post              # scan + post to Discord
    python3 scripts/run_daily_local.py --date 2026-10-07   # a specific session

Needs APCA_API_KEY_ID and APCA_API_SECRET_KEY, plus NANTWATCH_WEBHOOK_URL for --post.
"""
import argparse
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from alpaca_client import AlpacaClient  # noqa: E402
from discord_post import post_messages  # noqa: E402
from scanner import build_daily, load_trading_days, pick_report_day  # noqa: E402


def show_progress(done: int, total: int) -> None:
    print(f"\r  scanning batch {done}/{total}", end="", flush=True)
    if done == total:
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="NantWatch daily breakdown (local run)")
    parser.add_argument("--date", help="session to report, YYYY-MM-DD (default: latest finished)")
    parser.add_argument("--post", action="store_true", help="also post to Discord")
    args = parser.parse_args()

    key_id = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    if not key_id or not secret:
        print("ERROR: export APCA_API_KEY_ID and APCA_API_SECRET_KEY first.", file=sys.stderr)
        return 1
    client = AlpacaClient(key_id, secret)

    if args.date:
        report_day = date.fromisoformat(args.date)
    else:
        report_day = pick_report_day(load_trading_days(client, date.today()), datetime.now())
    if report_day is None:
        print("No finished trading session found.")
        return 1

    print(f"Building the daily breakdown for {report_day} ...")
    built = build_daily(client, report_day, on_batch=show_progress)
    if built is None:
        print(f"{report_day} was not a trading day, so there is nothing to report.")
        return 0
    result, messages = built
    print(result.summary())
    for number, message in enumerate(messages, start=1):
        print(f"--- message {number}/{len(messages)} ({len(message)} chars) ---")
        print(message)

    if not args.post:
        print("\nNot posted. Run again with --post to send it to Discord.")
        return 0
    webhook_url = os.environ.get("NANTWATCH_WEBHOOK_URL", "").strip()
    if not webhook_url.startswith("https://"):
        print("ERROR: export NANTWATCH_WEBHOOK_URL first.", file=sys.stderr)
        return 1
    print(f"Discord responded: {post_messages(webhook_url, messages)}  (204 = delivered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
