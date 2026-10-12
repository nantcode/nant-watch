"""M6: build the race scoreboard from your Mac (read-only).

    python3 scripts/race_local.py          # print only
    python3 scripts/race_local.py --post   # print AND post to Discord

Needs APCA_API_KEY_ID / APCA_API_SECRET_KEY (NantBot's account).
Every other racer in src/racers.json reads its keys from <NAME>_KEY_ID /
<NAME>_SECRET_KEY (e.g. SPARTICUS_KEY_ID), or the env names its entry sets.
NANTWATCH_WEBHOOK_URL is needed only for --post.
"""
import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from alpaca_client import AlpacaClient  # noqa: E402
from discord_post import post_messages  # noqa: E402
from formatting import fmt_pct, fmt_price, pack_messages  # noqa: E402
from market_math import trading_days_from_calendar  # noqa: E402
from race import RACE_START, build_scoreboard, load_entrants, make_race_accounts  # noqa: E402


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="NantWatch race scoreboard (local run)")
    parser.add_argument("--post", action="store_true", help="also post to Discord")
    args = parser.parse_args()

    if not env("APCA_API_KEY_ID") or not env("APCA_API_SECRET_KEY"):
        print("ERROR: export APCA_API_KEY_ID and APCA_API_SECRET_KEY first.", file=sys.stderr)
        return 1
    nantbot = AlpacaClient(env("APCA_API_KEY_ID"), env("APCA_API_SECRET_KEY"))

    entrants = load_entrants()
    keys = {e.name: (env(e.key_id_env), env(e.secret_key_env)) for e in entrants if not e.main}
    accounts = make_race_accounts(entrants, nantbot, env("APCA_API_KEY_ID"), keys, AlpacaClient, print)

    calendar = nantbot.get_calendar(RACE_START - timedelta(days=14), date.today())
    text, racers = build_scoreboard(accounts, nantbot, trading_days_from_calendar(calendar))

    for racer in racers:
        print(f"  {racer.name:<10} {fmt_price(racer.start_value):>13} -> {fmt_price(racer.end_value):>13}"
              f"  {fmt_pct(racer.return_pct):>8}  (through {racer.end_day})")
    messages = pack_messages([text])
    print()
    print(messages[0])

    if not args.post:
        print("\nNot posted. Run again with --post to send it to Discord.")
        return 0
    if not env("NANTWATCH_WEBHOOK_URL").startswith("https://"):
        print("ERROR: export NANTWATCH_WEBHOOK_URL first.", file=sys.stderr)
        return 1
    print(f"Discord responded: {post_messages(env('NANTWATCH_WEBHOOK_URL'), messages)}  (204 = delivered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
