"""Small things that make text of numbers and times, for what the pages say."""

from datetime import UTC, datetime


def pretty_size(number) -> str:
    """A number of bytes as text: 12 KB, 3.2 GB."""
    number = float(number or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1000 or unit == "TB":
            return (
                f"{number:.0f} {unit}"
                if unit == "B" or number >= 100
                else f"{number:.1f} {unit}"
            )
        number /= 1000
    return ""


def pretty_when(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else ""


def pretty_age(moment) -> str:
    """How long ago a time was (the times of the database are UTC, without a zone)."""
    if not moment:
        return ""
    seconds = max(
        0, int((datetime.now(UTC).replace(tzinfo=None) - moment).total_seconds())
    )
    for unit, span in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= span:
            return f"{seconds // span} {unit}"
    return f"{seconds} s"


def pretty_modified(when, now=None) -> str:
    """Text for how long ago a record was last changed (`Last modified 3 days ago`)."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    seconds = max(0, int((now - when).total_seconds()))
    days = seconds // 86400
    if days > 30:
        return f"Last modified on {when.strftime('%A, %B')} {when.day} {when.year}"

    def plural(n: int, unit: str) -> str:
        return f"{n} {unit}{'' if n == 1 else 's'} ago"

    if days:
        return "Last modified " + plural(days, "day")
    if seconds >= 3600:
        return "Last modified " + plural(seconds // 3600, "hour")
    if seconds >= 60:
        return "Last modified " + plural(seconds // 60, "minute")
    return "Last modified just now"
