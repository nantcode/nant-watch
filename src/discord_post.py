"""Tiny Discord webhook client built on the standard library only
(no `requests` install needed, locally or in Lambda).

The pure parts (build_payload, retry_after_seconds) are unit tested.
The network part (post_message) is kept as thin as possible.
"""
import json
import time
import urllib.error
import urllib.request
from typing import Callable, List, Sequence

from formatting import BOT_NAME, DISCORD_HARD_LIMIT

# Discord sits behind Cloudflare, which blocks Python's default
# "Python-urllib/3.x" User-Agent (you'd get HTTP 403, error 1010).
USER_AGENT = "BreakdownBot/0.1 (+https://github.com/nantcode/breakdown-bot)"


def build_payload(content: str, username: str = BOT_NAME) -> dict:
    """The JSON body Discord expects. Refuses empty or oversized messages."""
    if not content or not content.strip():
        raise ValueError("Discord message is empty")
    if len(content) > DISCORD_HARD_LIMIT:
        raise ValueError(
            f"Discord message is {len(content)} chars (limit {DISCORD_HARD_LIMIT})"
        )
    return {
        "content": content,
        "username": username,
        # Never ping @everyone, @here, roles or users, even if a ticker
        # or headline happens to contain an '@'.
        "allowed_mentions": {"parse": []},
    }


def retry_after_seconds(body: bytes, default: float = 1.0, cap: float = 10.0) -> float:
    """Read Discord's 429 response body ({"retry_after": 0.75, ...})
    and return how long to wait, clamped to a sane range."""
    try:
        wait = float(json.loads(body.decode("utf-8")).get("retry_after", default))
    except (ValueError, AttributeError, UnicodeDecodeError):
        wait = default
    return max(0.0, min(wait, cap))


def post_message(
    webhook_url: str,
    content: str,
    username: str = BOT_NAME,
    timeout: float = 10.0,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """POST one message. Returns the HTTP status (204 = success).
    Retries politely if Discord says we're going too fast (HTTP 429)."""
    data = json.dumps(build_payload(content, username)).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status
        except urllib.error.HTTPError as err:
            body = err.read()
            if err.code == 429 and attempt < max_attempts:
                sleep(retry_after_seconds(body))
                continue
            # Never include webhook_url in errors/logs: it is a secret.
            raise RuntimeError(
                f"Discord webhook failed: HTTP {err.code}: {body[:300]!r}"
            ) from None
    raise RuntimeError("Discord webhook failed: too many 429 retries")


def post_messages(
    webhook_url: str,
    messages: Sequence[str],
    pause_seconds: float = 1.0,
    sender: Callable[[str, str], int] = post_message,
    sleep: Callable[[float], None] = time.sleep,
) -> List[int]:
    """Send several messages in order, pausing between them so a multi-part
    report arrives in the right order and under Discord's rate limit.

    `sender` and `sleep` are parameters so tests can pass fakes
    (this is called *dependency injection*)."""
    statuses: List[int] = []
    for index, message in enumerate(messages):
        if index > 0:
            sleep(pause_seconds)
        statuses.append(sender(webhook_url, message))
    return statuses
