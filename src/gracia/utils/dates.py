"""Dates as people write them, for the locale of the repository (`gracia:locale`).

A date is stored as ISO text with its XSD datatype (`2024-03-15` is an xsd:date,
`2024-03` an xsd:gYearMonth, `2024` an xsd:gYear); the form shows and reads it in the
order and with the separator of the locale (15-03-2024 for nl-NL, 03/15/2024 for
en-US). ISO text is always understood, whatever the locale.
"""

import datetime
import re

from gracia import uri

XSD_DATE = uri.XSD_NS.date
XSD_YEAR_MONTH = uri.XSD_NS.gYearMonth
XSD_YEAR = uri.XSD_NS.gYear
KINDS = (XSD_DATE, XSD_YEAR_MONTH, XSD_YEAR)  # the datatypes the date widget handles
# locale -> (order of day, month and year, separator); "" is ISO 8601
FORMATS: dict[str, tuple[str, str]] = {
    "": ("ymd", "-"),
    "en-US": ("mdy", "/"),
    "en-GB": ("dmy", "/"),
    "nl-NL": ("dmy", "-"),
    "de-DE": ("dmy", "."),
    "fr-FR": ("dmy", "/"),
    "es-ES": ("dmy", "/"),
    "it-IT": ("dmy", "/"),
    "pt-BR": ("dmy", "/"),
    "ru-RU": ("dmy", "."),
    "ja-JP": ("ymd", "/"),
    "zh-CN": ("ymd", "/"),
}
NAMES = {
    "": "ISO 8601",
    "en-US": "English (United States)",
    "en-GB": "English (United Kingdom)",
    "nl-NL": "Dutch",
    "de-DE": "German",
    "fr-FR": "French",
    "es-ES": "Spanish",
    "it-IT": "Italian",
    "pt-BR": "Portuguese (Brazil)",
    "ru-RU": "Russian",
    "ja-JP": "Japanese",
    "zh-CN": "Chinese",
}
ISO = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
PARTS = re.compile(r"[-/.\s]+")
# the first day of the week of the locale (0: Sunday, 1: Monday); ISO weeks begin on a
# Monday
FIRST_DAY = {"en-US": 0, "pt-BR": 0, "ja-JP": 0, "zh-CN": 0}


def _format(locale: str) -> tuple[str, str]:
    return FORMATS.get(locale, FORMATS[""])


def _join(locale: str, year: str, month: str | None, day: str | None) -> str:
    order, sep = _format(locale)
    given = {"y": year, "m": month, "d": day}
    return sep.join(v for k in order if (v := given[k]) is not None)


def first_day(locale: str) -> int:
    return FIRST_DAY.get(locale, 1)


def tag(locale: str) -> str:
    """The language tag the calendar names its months and days by; English for ISO."""
    return locale or "en"


def layout(locale: str) -> tuple[str, str]:
    """The order of day, month and year (`dmy`) and the separator of the locale."""
    return _format(locale)


def example(locale: str) -> str:
    return _join(locale, "2024", "03", "15")


def label(locale: str) -> str:
    return f"{NAMES.get(locale, locale)} ({example(locale)})"


def datatype_of(iso: str) -> str | None:
    """The XSD datatype of ISO date text, or none when it is not one."""
    found = ISO.match(iso.strip()) if iso else None
    if not found:
        return None
    return KINDS[2 - sum(g is not None for g in found.groups()[1:])]


def _valid(year: str, month: str | None, day: str | None) -> bool:
    try:
        datetime.date(int(year), int(month or 1), int(day or 1))
    except ValueError:
        return False
    return int(year) > 0


def display(iso: str, locale: str) -> str:
    """ISO date text in the form of the locale; what is not a date is left as it is."""
    found = ISO.match(iso.strip()) if iso else None
    if not found or not _valid(*found.groups()):
        return iso
    return _join(locale, *found.groups())


def placeholder(locale: str, kinds=KINDS) -> str:
    if XSD_DATE in kinds:
        return (
            example(locale)
            .replace("2024", "yyyy")
            .replace("03", "mm")
            .replace("15", "dd")
        )
    if XSD_YEAR_MONTH in kinds:
        return _join(locale, "yyyy", "mm", None)
    return "yyyy"


def parse(text: str, locale: str, kinds=KINDS) -> str:
    """ISO text of a date typed in the form of the locale (or in ISO), or a ValueError
    that says what is wrong."""
    typed = text.strip()
    parts = PARTS.split(typed) if typed else []
    order = _format(locale)[0]
    if not parts or len(parts) > 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Not a date, like {example(locale)}")
    if len(parts[0]) == 4:  # ISO, or the order of the locale: year first
        order = "ymd"
    order = order.replace("d", "") if len(parts) == 2 else order
    if len(parts) == 1:
        order = "y"
    by = dict(zip(order, parts, strict=True))
    year, month, day = by.get("y"), by.get("m"), by.get("d")
    if year is None or len(year) != 4:
        raise ValueError(f"Give the year in four digits, like {example(locale)}")
    if (month and len(month) > 2) or (day and len(day) > 2):
        raise ValueError(f"Not a date, like {example(locale)}")
    month = month.zfill(2) if month else None
    day = day.zfill(2) if day else None
    if not _valid(year, month, day):
        raise ValueError("Not a date that exists")
    iso = "-".join(p for p in (year, month, day) if p)
    if datatype_of(iso) not in kinds:
        raise ValueError(f"Give it as {placeholder(locale, kinds)}")
    return iso
