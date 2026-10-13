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
from race import Entrant, load_entrants, make_race_accounts
from scanner import build_daily, load_trading_days, pick_report_day
from weekly import build_weekly
from sessions import MODES, build_session_post, load_session_calendar

# secret name -> environment variable that holds its SSM parameter path
REQUIRED_PARAMS = {
    "key_id": "ALPACA_KEY_ID_PARAM",
    "secret_key": "ALPACA_SECRET_KEY_PARAM",
    "webhook_url": "DISCORD_WEBHOOK_PARAM",
}
SSM_BATCH = 10   # GetParameters takes at most 10 names per call


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
    """Fetch all secrets in as few SSM calls as possible (10 names per call).
    A missing required secret fails loudly; a missing optional one (e.g. a
    racer's keys you haven't stored yet) is skipped."""
    names = list(dict.fromkeys(names))
    values: Dict[str, str] = {}
    invalid: List[str] = []
    for start in range(0, len(names), SSM_BATCH):
        response = ssm.get_parameters(Names=names[start:start + SSM_BATCH], WithDecryption=True)
        invalid.extend(response.get("InvalidParameters") or [])
        values.update({p["Name"]: p["Value"] for p in response["Parameters"]})
    missing = [n for n in invalid if n not in optional]
    if missing:
        raise RuntimeError(f"missing SSM parameters: {missing}")
    return values


def racer_params(entrants: Sequence[Entrant]) -> Dict[str, str]:
    """Secret key -> SSM path for every racer that brings its own keys."""
    params: Dict[str, str] = {}
    for entrant in entrants:
        if not entrant.main:
            params[entrant.key_id_secret] = entrant.key_id_param
            params[entrant.secret_key_secret] = entrant.secret_key_param
    return params


def secrets_from_ssm(ssm, environ: Mapping[str, str],
                     entrants: Optional[Sequence[Entrant]] = None) -> Dict[str, str]:
    """Read the SSM paths from environment variables and racers.json, then fetch
    them all at once. Returns friendly keys like 'key_id', 'webhook_url', 'sparticus_key_id'."""
    required = {key: environ[var] for key, var in REQUIRED_PARAMS.items()}
    optional = racer_params(load_entrants() if entrants is None else entrants)
    values = load_secrets(ssm, list(required.values()) + list(optional.values()),
                          optional=[n for n in optional.values() if n not in required.values()])
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
    if event.get("mode"):   # v1.1 weekday schedule: open / race / close
        return run_weekday(event, now_utc, secrets, make_client, poster, log)
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


# ---------- the weekday schedule (v1.1): open review, race updates, close review ----------

def make_accounts(secrets: Dict[str, str], make_client: Callable[[str, str], object],
                  log: Callable[[str], None], entrants: Optional[Sequence[Entrant]] = None):
    """NantBot's client (it also reads market data) + every racer in racers.json."""
    entrants = load_entrants() if entrants is None else entrants
    nantbot = make_client(secrets["key_id"], secrets["secret_key"])
    keys = {e.name: (secrets.get(e.key_id_secret, ""), secrets.get(e.secret_key_secret, ""))
            for e in entrants if not e.main}
    return nantbot, make_race_accounts(entrants, nantbot, secrets["key_id"], keys, make_client, log)


def run_weekday(
    event: dict,
    now_utc: datetime,
    secrets: Dict[str, str],
    make_client: Callable[[str, str], object] = AlpacaClient,
    poster: Callable[[str, List[str]], List[int]] = post_messages,
    log: Callable[[str], None] = print,
) -> dict:
    """One of the five weekday posts, picked by event['mode']: open, race or close."""
    mode = event.get("mode", "close")
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode} (use one of {', '.join(MODES)})")
    nantbot, accounts = make_accounts(secrets, make_client, log)
    now_et = eastern_now(now_utc)
    trading_days = load_session_calendar(nantbot, now_et.date())
    if event.get("source") == "schedule" and now_et.date() not in trading_days:
        log(f"{now_et.date()} is not a trading day, skipping the {mode} post")
        return {"status": "skipped", "mode": mode, "day": now_et.date().isoformat()}

    session_day = date.fromisoformat(event["date"]) if event.get("date") else None
    messages, summary = build_session_post(mode, nantbot, accounts, trading_days, now_et, session_day)
    log(summary)
    if messages is None:
        return {"status": "skipped", "mode": mode, "summary": summary}
    if event.get("dry_run"):
        return {"status": "dry_run", "mode": mode, "summary": summary, "messages": messages}
    statuses = poster(secrets["webhook_url"], messages)
    log(f"posted {len(messages)} {mode} message(s), Discord said {statuses}")
    return {"status": "posted", "mode": mode, "summary": summary, "discord": statuses}


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
    nantbot, accounts = make_accounts(secrets, make_client, log)

    as_of = date.fromisoformat(event["date"]) if event.get("date") else None
    built = build_weekly(nantbot, accounts, eastern_now(now_utc), as_of)
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
