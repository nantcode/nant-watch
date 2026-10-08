"""Step 1 smoke test: build a daily report from FAKE sample data and
(optionally) post it to your new Discord channel.

Run from the project root:
    python3 scripts/send_preview.py --dry-run   # print only, no network
    python3 scripts/send_preview.py             # print AND post (needs BREAKDOWN_WEBHOOK_URL)
"""
import argparse
import os
import sys

# Let this script import from src/ without installing anything.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from discord_post import post_messages  # noqa: E402
from formatting import Mover, build_daily_report  # noqa: E402

SAMPLE_INDEXES = [
    Mover("SPY", 672.41, 0.84, 61_200_000),
    Mover("QQQ", 598.10, -0.37, 38_900_000),
]
SAMPLE_GAINERS = [
    Mover("ACME", 18.42, 24.6, 9_800_000),
    Mover("WIDG", 7.15, 17.9, 3_400_000),
    Mover("FOOB", 112.30, 11.2, 2_100_000),
    Mover("BARQ", 54.88, 9.7, 5_600_000),
    Mover("ZZZT", 9.03, 8.4, 1_250_000),
]
SAMPLE_LOSERS = [
    Mover("GLMR", 6.21, -19.8, 7_700_000),
    Mover("DRPX", 31.47, -14.3, 2_900_000),
    Mover("SNKR", 12.90, -11.1, 4_050_000),
    Mover("FALL", 88.02, -8.6, 1_800_000),
    Mover("OOPS", 5.37, -7.2, 980_000),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview BreakdownBot's daily post")
    parser.add_argument("--dry-run", action="store_true", help="print only, don't post")
    args = parser.parse_args()

    messages = build_daily_report(
        "🧪 SAMPLE DATA (formatting test)", SAMPLE_INDEXES, SAMPLE_GAINERS, SAMPLE_LOSERS
    )
    for number, message in enumerate(messages, start=1):
        print(f"--- message {number}/{len(messages)} ({len(message)} chars) ---")
        print(message)

    if args.dry_run:
        print("\nDry run: nothing was sent.")
        return 0

    webhook_url = os.environ.get("BREAKDOWN_WEBHOOK_URL", "").strip()
    if not webhook_url.startswith("https://"):
        print("\nERROR: set BREAKDOWN_WEBHOOK_URL first (see Step 1.6).", file=sys.stderr)
        return 1

    statuses = post_messages(webhook_url, messages)
    print(f"\nDiscord responded: {statuses}  (204 = delivered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
