"""Philippine holiday helpers.

Primary source is a deterministic generator: fixed-date national holidays,
the Holy Week trio computed from Gregorian Easter, and National Heroes Day
(last Monday of August). This needs no network and is stable year to year.

If you self-host the PhilippineHolidayAPI (https://github.com/surelle-ha/
PhilippineHolidayAPI) set ``PH_HOLIDAY_API_URL`` and ``fetch_from_api`` will
pull its scraped Official Gazette list instead; on any failure we fall back to
the generator so the "Import" button never leaves the admin stuck.
"""

from __future__ import annotations

from datetime import date, timedelta

REGULAR = 'REGULAR'
SPECIAL = 'SPECIAL'
COLOR_REGULAR = '#DC2626'   # red
COLOR_SPECIAL = '#F59E0B'   # amber


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Computus)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _last_monday_of_august(year: int) -> date:
    d = date(year, 8, 31)
    return d - timedelta(days=(d.weekday()))  # weekday() Mon=0 -> back to Monday


def philippine_holidays(year: int) -> list[dict]:
    """Deterministic national holiday list for ``year``.

    Excludes lunar-calendar holidays (Chinese New Year, Eid'l Fitr, Eid'l Adha)
    since those are proclaimed yearly and can't be computed reliably — add those
    by hand. Returns dicts: {date, name, type, color}.
    """
    easter = _easter_sunday(year)
    maundy = easter - timedelta(days=3)
    good_friday = easter - timedelta(days=2)
    black_saturday = easter - timedelta(days=1)
    heroes = _last_monday_of_august(year)

    regular = [
        (date(year, 1, 1), "New Year's Day"),
        (date(year, 4, 9), 'Araw ng Kagitingan'),
        (maundy, 'Maundy Thursday'),
        (good_friday, 'Good Friday'),
        (date(year, 5, 1), 'Labor Day'),
        (date(year, 6, 12), 'Independence Day'),
        (heroes, 'National Heroes Day'),
        (date(year, 11, 30), 'Bonifacio Day'),
        (date(year, 12, 25), 'Christmas Day'),
        (date(year, 12, 30), 'Rizal Day'),
    ]
    special = [
        (black_saturday, 'Black Saturday'),
        (date(year, 8, 21), 'Ninoy Aquino Day'),
        (date(year, 11, 1), "All Saints' Day"),
        (date(year, 11, 2), "All Souls' Day"),
        (date(year, 12, 8), 'Feast of the Immaculate Conception'),
        (date(year, 12, 24), 'Christmas Eve'),
        (date(year, 12, 31), 'Last Day of the Year'),
    ]

    out = []
    for d, name in regular:
        out.append({'date': d, 'name': name, 'type': REGULAR, 'color': COLOR_REGULAR})
    for d, name in special:
        out.append({'date': d, 'name': name, 'type': SPECIAL, 'color': COLOR_SPECIAL})
    out.sort(key=lambda h: h['date'])
    return out


def fetch_from_api(year: int):
    """Try the self-hosted PhilippineHolidayAPI; return the same dict shape or
    None on any failure (caller falls back to :func:`philippine_holidays`)."""
    import json
    import urllib.request
    from datetime import datetime

    from django.conf import settings

    base = getattr(settings, 'PH_HOLIDAY_API_URL', '') or ''
    if not base:
        return None
    base = base.rstrip('/')
    try:
        with urllib.request.urlopen(f'{base}/holiday/{year}', timeout=8) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        raw = (payload.get('data') or {}).get('holidays') or []
        out = []
        for item in raw:
            # API date is an Official Gazette title string, e.g. "January 1, 2026".
            for fmt in ('%B %d, %Y', '%Y-%m-%d', '%b %d, %Y'):
                try:
                    d = datetime.strptime(item.get('date', ''), fmt).date()
                    break
                except (ValueError, TypeError):
                    d = None
            if d is None:
                continue
            out.append({'date': d, 'name': item.get('event') or 'Holiday',
                        'type': REGULAR, 'color': COLOR_REGULAR})
        return out or None
    except Exception:
        return None
