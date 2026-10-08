"""Read-only Alpaca client for NantWatch (standard library only).

Safety first: NantWatch must NEVER trade. Alpaca API keys are able to place
orders, so the read-only promise is enforced here in code, three ways:
  1. the network function can only send GET requests,
  2. every URL must be on an allowlist of read-only paths,
  3. any path mentioning orders or positions is refused outright.
The unit tests prove all three.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import date
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

TRADING_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
USER_AGENT = "NantWatch/0.3 (read-only)"

ALLOWED_PATHS = (
    "/v2/assets",
    "/v2/calendar",
    "/v2/stocks/bars",
    "/v2/account/portfolio/history",   # used in M6 for the race scoreboard
)
FORBIDDEN_WORDS = ("order", "position")

# A transport takes (url, headers, timeout) and returns (status_code, body_bytes).
Transport = Callable[[str, Dict[str, str], float], Tuple[int, bytes]]


class ReadOnlyViolation(Exception):
    """Raised if anything tries to make NantWatch do more than read."""


class AlpacaError(Exception):
    """Alpaca answered with an error we couldn't recover from."""


# ---------- pure helpers ----------

def check_read_only(method: str, url: str) -> None:
    """Raise ReadOnlyViolation unless this is a GET to an allowlisted path."""
    if method.upper() != "GET":
        raise ReadOnlyViolation(f"blocked {method} request: NantWatch is read-only")
    path = urllib.parse.urlparse(url).path.lower()
    if any(word in path for word in FORBIDDEN_WORDS):
        raise ReadOnlyViolation(f"blocked {path}: NantWatch never touches orders or positions")
    if not any(path == allowed or path.startswith(allowed + "/") for allowed in ALLOWED_PATHS):
        raise ReadOnlyViolation(f"blocked {path}: not on the read-only allowlist")


def build_url(base: str, path: str, params: Optional[Dict[str, object]] = None) -> str:
    """build_url(DATA_BASE, '/v2/stocks/bars', {'symbols': 'A,B', 'end': None})
    -> 'https://data.alpaca.markets/v2/stocks/bars?symbols=A%2CB' (None values are dropped)."""
    clean = {key: value for key, value in (params or {}).items() if value is not None}
    query = urllib.parse.urlencode(clean)
    return f"{base}{path}?{query}" if query else f"{base}{path}"


def chunked(items: Sequence[str], size: int) -> List[List[str]]:
    """chunked(['A','B','C'], 2) -> [['A','B'], ['C']]."""
    if size < 1:
        raise ValueError("size must be at least 1")
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


# ---------- rate limiting ----------

class RateLimiter:
    """Allow at most `max_calls` in any rolling `period` seconds.
    Alpaca's free plan allows 200 requests/min; we default to 180 for headroom.
    clock and sleep are parameters so tests can fake time (no real waiting)."""

    def __init__(
        self,
        max_calls: int = 180,
        period: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: Deque[float] = deque()

    def wait(self) -> None:
        """Call right before each request. Sleeps only if we're at the limit."""
        now = self._clock()
        while self._calls and now - self._calls[0] >= self.period:
            self._calls.popleft()               # forget calls older than the window
        if len(self._calls) >= self.max_calls:
            pause = self.period - (now - self._calls[0])
            if pause > 0:
                self._sleep(pause)
            now = self._clock()
            self._calls.popleft()
        self._calls.append(now)


# ---------- the only code that touches the network ----------

def urllib_transport(url: str, headers: Dict[str, str], timeout: float = 30.0) -> Tuple[int, bytes]:
    """Send one GET request. The method is hard-coded: this function cannot POST."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


# ---------- the client ----------

class AlpacaClient:
    def __init__(
        self,
        key_id: str,
        secret_key: str,
        transport: Transport = urllib_transport,
        limiter: Optional[RateLimiter] = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
        backoff_seconds: float = 2.0,
    ):
        if not key_id or not secret_key:
            raise ValueError("Alpaca key id and secret key are required")
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        self._secret = secret_key
        self._transport = transport
        self._limiter = limiter or RateLimiter()
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self.request_count = 0

    def _get(self, base: str, path: str, params: Optional[Dict[str, object]] = None):
        url = build_url(base, path, params)
        check_read_only("GET", url)
        for attempt in range(1, self._max_attempts + 1):
            self._limiter.wait()
            self.request_count += 1
            status, body = self._transport(url, self._headers, 30.0)
            if status == 200:
                return json.loads(body.decode("utf-8"))
            retryable = status == 429 or status >= 500
            if retryable and attempt < self._max_attempts:
                self._sleep(self._backoff * attempt)   # wait 2s, 4s, 6s ... then retry
                continue
            message = body[:300].decode("utf-8", "replace").replace(self._secret, "***")
            raise AlpacaError(f"Alpaca GET {path} failed: HTTP {status}: {message}")
        raise AlpacaError(f"Alpaca GET {path} failed after {self._max_attempts} attempts")

    def get_assets(self) -> List[dict]:
        """Every active US equity Alpaca knows about (one big list)."""
        return self._get(TRADING_BASE, "/v2/assets", {"status": "active", "asset_class": "us_equity"})

    def get_calendar(self, start: date, end: date) -> List[dict]:
        """Market sessions between two dates (holidays are simply missing)."""
        return self._get(TRADING_BASE, "/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})

    def get_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: Optional[date] = None,
        timeframe: str = "1Day",
        feed: str = "sip",
        adjustment: str = "split",
        limit: int = 10000,
    ) -> Dict[str, List[dict]]:
        """Bars for many symbols in one request, following next_page_token
        until Alpaca says there are no more pages.
        end=None lets Alpaca use its default (now, minus 15 minutes on the free plan).
        adjustment='split' stops a stock split from looking like a -90% crash."""
        merged: Dict[str, List[dict]] = {}
        page_token: Optional[str] = None
        while True:
            data = self._get(DATA_BASE, "/v2/stocks/bars", {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
                "feed": feed,
                "adjustment": adjustment,
                "limit": limit,
                "page_token": page_token,
            })
            for symbol, bars in (data.get("bars") or {}).items():
                merged.setdefault(symbol, []).extend(bars)
            page_token = data.get("next_page_token")
            if not page_token:
                return merged

    def get_bars_batched(
        self,
        symbols: Sequence[str],
        start: date,
        end: Optional[date] = None,
        batch_size: int = 200,
    ) -> Dict[str, List[dict]]:
        """Same as get_bars, but splits thousands of symbols into batches
        so each URL stays a sensible length."""
        merged: Dict[str, List[dict]] = {}
        for batch in chunked(list(symbols), batch_size):
            for symbol, bars in self.get_bars(batch, start, end).items():
                merged.setdefault(symbol, []).extend(bars)
        return merged
