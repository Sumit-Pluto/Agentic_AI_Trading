"""Exchange session windows and holiday calendar (IST).

Two consumers:
  - the persistent-order sweeper — "may I submit a fresh DAY order right now?"
    (`is_session_open`, conservative: unknown venue → closed).
  - the tick-driven target auto-exit gate — "may I fire an exit right now?"
    (`is_market_open_for_orders`, fails OPEN for unknown venues so adding a new
    exchange never silently disables its auto-exit, and honours the master
    enforce toggle).

Baked-in defaults ship below and are correct year-round on their own (MCX's
evening close auto-tracks US daylight saving). The Settings → Market Hours modal
can override any exchange's window and extend the holiday list; those overrides
are loaded from the DB into the in-memory layer at startup and on every save
(see `app.services.market_hours`). This module stays DB-free to avoid an import
cycle — the app pushes config in via `set_session_overrides` /
`set_holiday_overrides` / `set_enforced`.

Update `_NSE_HOLIDAYS` yearly (or add the new year's dates via the modal).
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# (open, close) in IST — a single continuous window per venue. Good enough for
# both consumers: they only need "is trading happening right now?". MCX's close
# here is the winter value; `_default_session` swaps in the summer value while
# US daylight saving is in effect (see `_mcx_close`).
_DEFAULT_SESSIONS: dict[str, tuple[time, time]] = {
    "NSE": (time(9, 15), time(15, 30)),
    "BSE": (time(9, 15), time(15, 30)),
    "NFO": (time(9, 15), time(15, 30)),
    "BFO": (time(9, 15), time(15, 30)),
    "CDS": (time(9, 0),  time(17, 0)),
    "BCD": (time(9, 0),  time(17, 0)),
    "MCX": (time(9, 0),  time(23, 30)),
}

# NSE cash + F&O holidays. MCX is a superset (skips a couple of days NSE trades)
# — for a conservative sweeper, we treat all listed dates as closed for every
# equity venue. MCX-specific dates are added into `_MCX_EXTRA`.
_NSE_HOLIDAYS: set[date] = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 20),   # Eid-ul-Fitr (tentative)
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 27),   # Eid-al-Adha (tentative)
    date(2026, 8, 15),   # Independence Day (Saturday — market usually closed anyway)
    date(2026, 8, 26),   # Ganesh Chaturthi (tentative)
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra (tentative)
    date(2026, 11, 9),   # Diwali - Laxmi Pujan (Muhurat trading only — treat as closed)
    date(2026, 11, 10),  # Diwali Balipratipada
    date(2026, 11, 24),  # Guru Nanak Jayanti (tentative)
    date(2026, 12, 25),  # Christmas
}

# MCX-specific closures beyond the NSE list (very few; the two calendars overlap heavily).
_MCX_EXTRA: set[date] = set()


# ---------------------------------------------------------------------------
# In-memory override layer — populated from the DB by app.services.market_hours
# at startup and after every Settings save. Empty = pure defaults.
# ---------------------------------------------------------------------------

# exch → (open, close). A present entry fully replaces that venue's default
# window (the user pinned it, so MCX DST auto-adjust no longer applies to it).
_session_overrides: dict[str, tuple[time, time]] = {}
# Extra holidays the user added — additive to the baked-in national list.
_holiday_overrides: set[date] = set()
# Master switch for the order gate only. When False, `is_market_open_for_orders`
# always allows — an escape hatch for special sessions the table doesn't cover.
# Does NOT affect the persistent sweeper (`is_session_open`), which must stay
# time-bounded regardless.
_enforced: bool = True


def set_session_overrides(overrides: dict[str, tuple[time, time]]) -> None:
    _session_overrides.clear()
    _session_overrides.update(overrides)


def set_holiday_overrides(dates: set[date]) -> None:
    _holiday_overrides.clear()
    _holiday_overrides.update(dates)


def set_enforced(enabled: bool) -> None:
    global _enforced
    _enforced = enabled


def is_enforced() -> bool:
    return _enforced


def _us_dst(d: date) -> bool:
    """True if US daylight saving is in effect on `d` (approx, date-only).

    US DST runs from the 2nd Sunday of March to the 1st Sunday of November.
    MCX's internationally-linked evening session tracks this: it closes 23:55
    IST while US DST is on, 23:30 otherwise.
    """
    year = d.year
    # 2nd Sunday of March: the 8th..14th that lands on a Sunday.
    march_start = next(day for day in range(8, 15) if date(year, 3, day).weekday() == 6)
    # 1st Sunday of November: the 1st..7th that lands on a Sunday.
    nov_end = next(day for day in range(1, 8) if date(year, 11, day).weekday() == 6)
    return date(year, 3, march_start) <= d < date(year, 11, nov_end)


def _mcx_close(d: date) -> time:
    return time(23, 55) if _us_dst(d) else time(23, 30)


def _default_session(exch: str, d: date) -> tuple[time, time] | None:
    """Baked-in window for `exch` on date `d`, before user overrides. MCX's
    close is date-dependent (US DST); everything else is static."""
    win = _DEFAULT_SESSIONS.get(exch)
    if win is None:
        return None
    if exch == "MCX":
        return (win[0], _mcx_close(d))
    return win


def effective_session(exch: str, d: date | None = None) -> tuple[time, time] | None:
    """The window actually in force for `exch` on `d`: user override if set,
    else the (DST-aware) default. None for venues we have no window for."""
    d = d or today_ist()
    if exch in _session_overrides:
        return _session_overrides[exch]
    return _default_session(exch, d)


def known_exchanges() -> list[str]:
    """Every venue with a window (default or override), sorted."""
    return sorted(set(_DEFAULT_SESSIONS) | set(_session_overrides))


def _holidays_for(exch: str) -> set[date]:
    base = _NSE_HOLIDAYS | _holiday_overrides
    if exch == "MCX":
        return base | _MCX_EXTRA
    return base


def is_trading_day(exch: str, d: date) -> bool:
    """Weekdays that aren't on the holiday list are trading days."""
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return d not in _holidays_for(exch)


def _to_ist(now: datetime | None) -> datetime:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def is_session_open(exch: str, now: datetime | None = None) -> bool:
    """True if `exch` is currently within its trading window (IST).

    Conservative: an exchange with no window returns False. Used by the
    persistent-order sweeper, which must never submit outside known hours.
    """
    now = _to_ist(now)
    if not is_trading_day(exch, now.date()):
        return False
    win = effective_session(exch, now.date())
    if win is None:
        return False
    open_t, close_t = win
    return open_t <= now.time() <= close_t


def is_market_open_for_orders(exch: str, now: datetime | None = None) -> bool:
    """Gate for tick-driven order placement (target auto-exit).

    Differs from `is_session_open` in two ways:
      - honours the master `_enforced` toggle (off → always allow);
      - fails OPEN for exchanges we have no window for, so trading a venue that
        isn't in the table never silently disables its auto-exit. Known venues
        are still enforced against their window.
    """
    if not _enforced:
        return True
    if exch not in _DEFAULT_SESSIONS and exch not in _session_overrides:
        return True  # unknown venue — don't block what we can't schedule
    return is_session_open(exch, now)


def today_ist() -> date:
    return datetime.now(IST).date()
