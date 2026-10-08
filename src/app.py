"""AWS Lambda entry points for NantWatch.

daily_handler  - weekday end-of-day breakdown (M5)
weekly_handler - Saturday weekly breakdown + race scoreboard (M7)

Event options (all optional, combine as you like):
  {"source": "schedule"}   sent by EventBridge Scheduler
  {"date": "2026-10-07"}   report one specific session (daily) / week containing it (weekly)
  {"dry_run": true}        build everything but do NOT post to Discord
  {}                       manual run: the latest finished session
"""
import os
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from alpaca_client import AlpacaClient
from discord_post import post_messages
from scanner import build_daily, load_trading_days, pick_report_day
from weekly import build_weekly

# secret name -> environment variable that holds its SSM parameter path
REQUIRED_PARAMS = {
    "key_id": "ALPACA_KEY_ID_PARAM",
    "secret_key": "ALPACA_SECRET_KEY_PARAM",
    "webhook_url": "DISCORD_WEBHOOK_PARAM",
}
OPTIONAL_PARAMS = {
    "sparticus_key_id": "SPARTICUS_KEY_ID_PARAM",
    "sparticus_secret_key": "SPARTICUS_SECRET_KEY_PARAM",
}


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


# ---------- secrets ----------

def load_secrets(ssm, names: Sequence[str], optional: Sequence[str] = ()) -> Dict[str, str]:
    """Fetch all secrets in ONE SSM call. A missing required secret fails loudly;
    a missing optional one (e.g. Sparticus keys you haven't stored) is skipped."""
    response = ssm.get_parameters(Names=list(names), WithDecryption=True)
    missing = [n for n in (response.get("InvalidParameters") or []) if n not in optional]
    if missing:
        raise RuntimeError(f"missing SSM parameters: {missing}")
    return {p["Name"]: p["Value"] for p in response["Parameters"]}


def secrets_from_ssm(ssm, environ: Mapping[str, str]) -> Dict[str, str]:
    """Read the SSM paths from environment variables, then fetch them all at once.
    Returns friendly keys like 'key_id', 'webhook_url', 'sparticus_key_id'."""
    required = {key: environ[var] for key, var in REQUIRED_PARAMS.items()}
    optional = {key: environ[var] for key, var in OPTIONAL_PARAMS.items() if environ.get(var)}
    values = load_secrets(ssm, list(required.values()) + list(optional.values()),
                          optional=list(optional.values()))
    secrets = {key: values[name] for key, name in required.items()}
    secrets.update({key: values[name] for key, name in optional.items() if name in values})
    return secrets


# ---------- the daily job (M5) ----------

def choose_report_day(event: dict, trading_days: Sequence[date], now_et: datetime) -> Optional[date]:
    """Which session should the daily run report on? None means 'skip'."""
    if event.get("date"):
        return date.fromisoformat(event["date"])
    if event.get("source") == "schedule":
        today = now_et.date()
        return today if today in trading_days else None   # holiday -> stay quiet
    return pick_report_day(trading_days, now_et)


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


# ---------- the weekly job (M7) ----------

def run_weekly(
    event: dict,
    now_utc: datetime,
    secrets: Dict[str, str],
    make_client: Callable[[str, str], object] = AlpacaClient,
    poster: Callable[[str, List[str]], List[int]] = post_messages,
    log: Callable[[str], None] = print,
) -> dict:
    """Weekly movers + race scoreboard. NantBot's keys also read market data."""
    nantbot = make_client(secrets["key_id"], secrets["secret_key"])
    sparticus = None
    if secrets.get("sparticus_key_id") and secrets.get("sparticus_secret_key"):
        if secrets["sparticus_key_id"] == secrets["key_id"]:
            log("WARNING: Sparticus and NantBot share one Alpaca account; their results will match")
        sparticus = make_client(secrets["sparticus_key_id"], secrets["sparticus_secret_key"])
    else:
        log("no Sparticus keys in SSM, scoreboard shows NantBot vs SPY")

    as_of = date.fromisoformat(event["date"]) if event.get("date") else None
    built = build_weekly(nantbot, {"NantBot": nantbot, "Sparticus": sparticus},
                         eastern_now(now_utc), as_of)
    if built is None:
        log("no finished trading week found, skipping")
        return {"status": "skipped"}
    result, messages, racers = built
    log(result.summary())
    for racer in racers:
        log(f"race: {racer.name} {racer.start_value:.2f} -> {racer.end_value:.2f} "
            f"({racer.return_pct:+.2f}%) through {racer.end_day}")

    week = f"{result.window.days[0]}..{result.window.end_day}"
    if event.get("dry_run"):
        return {"status": "dry_run", "week": week, "summary": result.summary(), "messages": messages}
    statuses = poster(secrets["webhook_url"], messages)
    log(f"posted {len(messages)} message(s), Discord said {statuses}")
    return {"status": "posted", "week": week, "summary": result.summary(), "discord": statuses}


# ---------- failure alerts (M8) ----------

def alert_text(job: str, error: Exception, now_et: datetime) -> str:
    """The Discord message we post when a run crashes. Short, no secrets."""
    detail = f"{type(error).__name__}: {error}".replace("`", "'")
    if len(detail) > 300:
        detail = detail[:297] + "..."
    when = f"{now_et:%a %b} {now_et.day}, {now_et:%I:%M %p} ET"
    return (f"⚠️ **NantWatch {job} run failed** · {when}\n"
            f"```\n{detail}\n```\n"
            f"Logs: /aws/lambda/nant-watch-{job}")


def run_with_alert(job: str, job_fn: Callable[..., dict], event: dict, now_utc: datetime,
                   secrets: Dict[str, str], poster: Optional[Callable] = None,
                   log: Optional[Callable[[str], None]] = None) -> dict:
    """Run a job. If it crashes: log it, post a warning to Discord, then re-raise
    so Lambda still records the error (that's what trips the CloudWatch alarm)."""
    log = log or print
    try:
        return job_fn(event, now_utc, secrets)
    except Exception as error:
        log(f"ERROR: {job} run failed: {type(error).__name__}: {error}")
        try:
            (poster or post_messages)(secrets["webhook_url"], [alert_text(job, error, eastern_now(now_utc))])
        except Exception as alert_error:
            log(f"could not post the failure alert either: {type(alert_error).__name__}")
        raise


# ---------- what Lambda calls ----------

def daily_handler(event, context):
    import boto3  # only exists inside Lambda; imported here so local tests never need it

    secrets = secrets_from_ssm(boto3.client("ssm"), os.environ)
    return run_with_alert("daily", run_daily, event or {}, datetime.now(timezone.utc), secrets)


def weekly_handler(event, context):
    import boto3

    secrets = secrets_from_ssm(boto3.client("ssm"), os.environ)
    return run_with_alert("weekly", run_weekly, event or {}, datetime.now(timezone.utc), secrets)
