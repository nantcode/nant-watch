"""Pure formatting helpers for BreakdownBot's Discord posts.

Every function here is a *pure function*: the same input always gives the
same output, and nothing touches the network, AWS, files, or the clock.
That is what makes them easy (and fast) to unit test.
"""
from dataclasses import dataclass
from datetime import date
from typing import List, Sequence

BOT_NAME = "BreakdownBot"
DISCORD_HARD_LIMIT = 2000   # Discord rejects any message longer than this
SAFE_LIMIT = 1900           # we aim lower to leave headroom (emoji, edits)
CODE_FENCE = "```"
TRUNCATION_NOTE = "\n…(truncated)"


@dataclass(frozen=True)
class Mover:
    """One row of market data we want to show.

    change_pct is a percent: 3.25 means +3.25%, -1.5 means -1.5%.
    volume is shares traded (daily report) or average daily shares (weekly).
    """
    symbol: str
    price: float
    change_pct: float
    volume: float


# ---------- small value formatters ----------

def fmt_pct(value: float, decimals: int = 2) -> str:
    """3.14159 -> '+3.14%', -0.5 -> '-0.50%', 0 -> '0.00%'."""
    rounded = round(value, decimals)
    if rounded == 0:
        rounded = 0.0  # turns -0.0 into 0.0 so we never print '-0.00%'
    sign = "+" if rounded > 0 else ""  # negatives already carry '-'
    return f"{sign}{rounded:.{decimals}f}%"


def fmt_price(value: float) -> str:
    """1234.5 -> '$1,234.50'."""
    return f"${value:,.2f}"


def fmt_volume(shares: float) -> str:
    """950 -> '950', 12_300 -> '12.3K', 4_100_000 -> '4.1M', 2.5e9 -> '2.5B'."""
    value = float(shares)
    if value < 1_000:
        return str(int(value))
    for size, unit in ((1e3, "K"), (1e6, "M"), (1e9, "B")):
        scaled = round(value / size, 1)
        if scaled < 1000 or unit == "B":   # 999,999 -> '1.0M', not '1000.0K'
            return f"{scaled:.1f}{unit}"
    return str(int(value))  # unreachable, keeps type checkers happy


def fmt_date_label(day: date) -> str:
    """date(2026, 10, 6) -> 'Tue Oct 6, 2026'."""
    return f"{day:%a %b} {day.day}, {day.year}"


def trend_emoji(change_pct: float) -> str:
    """Green for up, red for down, white for flat."""
    if change_pct > 0:
        return "🟢"
    if change_pct < 0:
        return "🔴"
    return "⚪"


# ---------- building blocks ----------

def market_summary(indexes: Sequence[Mover]) -> str:
    """One line per index, e.g. '🟢 **SPY** $672.41 (+0.84%)'."""
    if not indexes:
        return "(no data)"
    return "\n".join(
        f"{trend_emoji(m.change_pct)} **{m.symbol}** {fmt_price(m.price)} ({fmt_pct(m.change_pct)})"
        for m in indexes
    )


def movers_table(movers: Sequence[Mover]) -> str:
    """Ranked table inside a code block so the columns line up in Discord."""
    if not movers:
        return "(no data)"
    lines = [f"{'#':>2} {'SYM':<6} {'PRICE':>10} {'CHG':>8} {'VOL':>7}"]
    for rank, m in enumerate(movers, start=1):
        lines.append(
            f"{rank:>2} {m.symbol:<6} {fmt_price(m.price):>10} "
            f"{fmt_pct(m.change_pct, 1):>8} {fmt_volume(m.volume):>7}"
        )
    return CODE_FENCE + "\n" + "\n".join(lines) + "\n" + CODE_FENCE


def section(title: str, body: str) -> str:
    return f"**{title}**\n{body}"


# ---------- Discord size safety ----------

def truncate(text: str, limit: int = SAFE_LIMIT) -> str:
    """Shorten text to at most `limit` chars, cutting on a line break,
    closing any open code block, and adding a '(truncated)' note."""
    if len(text) <= limit:
        return text
    reserve = len(TRUNCATION_NOTE) + len("\n" + CODE_FENCE)
    if limit <= reserve:
        return text[:limit]
    cut = text[: limit - reserve]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n")]          # don't chop a table row in half
    if cut.count(CODE_FENCE) % 2 == 1:         # we stopped inside a code block
        cut += "\n" + CODE_FENCE
    return cut + TRUNCATION_NOTE


def pack_messages(sections: Sequence[str], limit: int = SAFE_LIMIT) -> List[str]:
    """Join sections (separated by blank lines) into as few Discord messages
    as possible. A section is never split across two messages; a section
    that is too big on its own gets truncated."""
    messages: List[str] = []
    current = ""
    for raw in sections:
        part = truncate(raw.strip(), limit)
        if not part:
            continue
        candidate = part if not current else current + "\n\n" + part
        if len(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = part
    if current:
        messages.append(current)
    return messages


# ---------- full reports ----------

def build_daily_report(
    date_label: str,
    indexes: Sequence[Mover],
    gainers: Sequence[Mover],
    losers: Sequence[Mover],
) -> List[str]:
    """Return the end-of-day breakdown as a list of Discord-safe messages."""
    sections = [
        f"📊 **{BOT_NAME} · Daily Breakdown** · {date_label}",
        section("Market", market_summary(indexes)),
        section("🚀 Top Gainers", movers_table(gainers)),
        section("📉 Top Losers", movers_table(losers)),
        "_Read-only market recap. Not financial advice._",
    ]
    return pack_messages(sections)
