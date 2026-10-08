"""AWS Lambda entry point for NantWatch's daily breakdown.

Event options (all optional, combine as you like):
  {"source": "schedule"}   sent by EventBridge Scheduler: report TODAY, skip holidays
  {"date": "2026-10-07"}   report one specific session
  {"dry_run": true}        build everything but do NOT post to Discord
  {}                       manual run: the latest finished session
"""
import os
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

from alpaca_client import AlpacaClient
from discord_post import post_messages
from scanner import build_daily, load_trading_days, pick_report_day

PARAM_ENV_VARS = ("ALPACA_KEY_ID_PARAM", "ALPACA_SECRET_KEY_PARAM", "DISCORD_WEBHOOK_PARAM")


# ---------- New York time without any extra packages ----------

def eastern_offset_hours(now_utc: datetime) -> int:
    """-4 during US daylight saving time, otherwise -5.
    DST runs from the 2nd Sunday of March at 2 AM (07:00 UTC)
    to the 1st Sunday of November at 2 AM (06:00 UTC)."""
    year = now_utc.year
    march_1 = date(year, 3, 1)
    second_sunday_march = march_1 + timedelta(days=(6 - march_1.weekday()) % 7 + 7)
    november_1 = date(year, 11, 1)
    first_sunday_november = november_1 + timedelta(days=(6 - november_1.weekday()) % 7)
    dst_start = datetime(year, 3, second_sunday_march.day, 7, tzinfo=timezone.utc)
    dst_end = datetime(year, 11, first_sunday_november.day, 6, tzinfo=timezone.utc)
    return -4 if dst_start <= now_utc < dst_end else -5


def eastern_now(now_utc: datetime) -> datetime:
    """UTC time -> New York wall-clock time (returned without tzinfo)."""
    local = now_utc + timedelta(hours=eastern_offset_hours(now_utc))
    return local.replace(tzinfo=None)


# ---------- deciding what to report ----------

def choose_report_day(event: dict, trading_days: Sequence[date], now_et: datetime) -> Optional[date]:
    """Which session should this run report on? None means 'skip'."""
    if event.get("date"):
        return date.fromisoformat(event["date"])
    if event.get("source") == "schedule":
        today = now_et.date()
        return today if today in trading_days else None   # holiday -> stay quiet
    return pick_report_day(trading_days, now_et)


def load_secrets(ssm, names: Sequence[str]) -> Dict[str, str]:
    """Fetch all secrets in ONE SSM call. Fails loudly if any are missing."""
    response = ssm.get_parameters(Names=list(names), WithDecryption=True)
    missing = response.get("InvalidParameters") or []
    if missing:
        raise RuntimeError(f"missing SSM parameters: {missing}")
    return {p["Name"]: p["Value"] for p in response["Parameters"]}


# ---------- the job ----------

def run_daily(
    event: dict,
    now_utc: datetime,
    secrets: Dict[str, str],
    make_client: Callable[[str, str], object] = AlpacaClient,
    poster: Callable[[str, List[str]], List[int]] = post_messages,
    log: Callable[[str], None] = print,
) -> dict:
    """The whole daily job. Every outside dependency is a parameter,
    so the tests can swap in fakes for Alpaca, Discord and the clock."""
    client = make_client(secrets["key_id"], secrets["secret_key"])
    now_et = eastern_now(now_utc)
    trading_days = load_trading_days(client, now_et.date())
    report_day = choose_report_day(event, trading_days, now_et)
    if report_day is None:
        log(f"{now_et.date()} is not a trading day, skipping")
        return {"status": "skipped", "day": now_et.date().isoformat()}

    built = build_daily(client, report_day)
    if built is None:
        log(f"{report_day} is not a trading day, skipping")
        return {"status": "skipped", "day": report_day.isoformat()}
    result, messages = built
    log(result.summary())

    if event.get("dry_run"):
        return {"status": "dry_run", "day": report_day.isoformat(),
                "summary": result.summary(), "preview": messages[0][:600]}
    statuses = poster(secrets["webhook_url"], messages)
    log(f"posted {len(messages)} message(s), Discord said {statuses}")
    return {"status": "posted", "day": report_day.isoformat(),
            "summary": result.summary(), "discord": statuses}


def daily_handler(event, context):
    """What Lambda calls. Wires the real AWS pieces into run_daily."""
    import boto3  # only exists inside Lambda; imported here so local tests never need it

    names = [os.environ[var] for var in PARAM_ENV_VARS]
    values = load_secrets(boto3.client("ssm"), names)
    secrets = {
        "key_id": values[names[0]],
        "secret_key": values[names[1]],
        "webhook_url": values[names[2]],
    }
    return run_daily(event or {}, datetime.now(timezone.utc), secrets)
