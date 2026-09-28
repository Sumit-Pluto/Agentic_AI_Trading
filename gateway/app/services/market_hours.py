"""Market-hours settings: bridge between the DB (MarketHours / MarketHoliday /
AppSetting) and the in-memory config `session_windows` reads on every tick.

`load_market_hours_config` is called once at startup and after every settings
mutation, so the tick-driven gate always sees the current windows without a DB
hit per tick.
"""
import logging
from datetime import date, datetime, time

from session_windows import (
    _default_session,
    effective_session,
    is_enforced,
    known_exchanges,
    set_enforced,
    set_holiday_overrides,
    set_session_overrides,
    today_ist,
)
from app.schemas import ExchangeHours, HolidayItem, MarketHoursSettings
from db.engine import get_db
from db.models import AppSetting, MarketHoliday, MarketHours

logger = logging.getLogger(__name__)

_ENFORCED_KEY = "market_hours_enforced"


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _fmt(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def load_market_hours_config() -> None:
    """Read all market-hours settings from the DB into `session_windows`.

    Skips silently on any error — the baked-in defaults remain in force, which
    is the safe fallback (correct standard windows, gate enforced).
    """
    db = next(get_db())
    try:
        overrides: dict[str, tuple[time, time]] = {}
        for row in db.query(MarketHours).all():
            try:
                overrides[row.exch] = (_parse_hhmm(row.open_time), _parse_hhmm(row.close_time))
            except (ValueError, AttributeError):
                logger.warning("Skipping malformed market_hours row for %s: %s-%s",
                               row.exch, row.open_time, row.close_time)

        holidays: set[date] = set()
        for row in db.query(MarketHoliday).all():
            try:
                holidays.add(date.fromisoformat(row.holiday))
            except ValueError:
                logger.warning("Skipping malformed market_holiday: %s", row.holiday)

        enforced_row = db.query(AppSetting).filter_by(key=_ENFORCED_KEY).first()
        enforced = enforced_row.value != "0" if enforced_row else True

        set_session_overrides(overrides)
        set_holiday_overrides(holidays)
        set_enforced(enforced)
        logger.info("Market-hours config loaded: %d override(s), %d extra holiday(s), enforced=%s",
                    len(overrides), len(holidays), enforced)
    except Exception:
        logger.exception("load_market_hours_config failed — keeping defaults")
    finally:
        db.close()


def get_market_hours_settings() -> MarketHoursSettings:
    """Build the Settings-modal DTO: every known exchange with its effective and
    default window (so the UI can flag overrides and offer 'reset'), plus the
    extra holidays and the enforce flag."""
    today = today_ist()
    exchanges: list[ExchangeHours] = []
    db = next(get_db())
    try:
        override_exchs = {r.exch for r in db.query(MarketHours).all()}
        for exch in known_exchanges():
            eff = effective_session(exch, today)
            dft = _default_session(exch, today)
            if eff is None or dft is None:
                continue
            exchanges.append(ExchangeHours(
                exch=exch,
                open=_fmt(eff[0]), close=_fmt(eff[1]),
                default_open=_fmt(dft[0]), default_close=_fmt(dft[1]),
                is_custom=exch in override_exchs,
            ))
        holidays = [
            HolidayItem(date=r.holiday, label=r.label or "")
            for r in db.query(MarketHoliday).order_by(MarketHoliday.holiday.asc()).all()
        ]
    finally:
        db.close()
    return MarketHoursSettings(enforced=is_enforced(), exchanges=exchanges, holidays=holidays)


def set_enforced_setting(enabled: bool) -> None:
    """Persist the enforce toggle, then refresh the in-memory config."""
    db = next(get_db())
    try:
        row = db.query(AppSetting).filter_by(key=_ENFORCED_KEY).first()
        if row is None:
            row = AppSetting(key=_ENFORCED_KEY, value="1" if enabled else "0")
            db.add(row)
        else:
            row.value = "1" if enabled else "0"
        db.commit()
    finally:
        db.close()
    load_market_hours_config()


def upsert_exchange_hours(exch: str, open_time: str, close_time: str) -> None:
    """Persist a per-exchange window override, then refresh in-memory config."""
    db = next(get_db())
    try:
        row = db.query(MarketHours).filter_by(exch=exch).first()
        if row is None:
            row = MarketHours(exch=exch, open_time=open_time, close_time=close_time)
            db.add(row)
        else:
            row.open_time = open_time
            row.close_time = close_time
            row.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    load_market_hours_config()


def reset_exchange_hours(exch: str) -> None:
    """Drop the override for `exch` (revert to the baked-in default)."""
    db = next(get_db())
    try:
        db.query(MarketHours).filter_by(exch=exch).delete()
        db.commit()
    finally:
        db.close()
    load_market_hours_config()


def add_holiday(holiday: str, label: str = "") -> None:
    db = next(get_db())
    try:
        existing = db.query(MarketHoliday).filter_by(holiday=holiday).first()
        if existing is None:
            db.add(MarketHoliday(holiday=holiday, label=label))
        else:
            existing.label = label
        db.commit()
    finally:
        db.close()
    load_market_hours_config()


def remove_holiday(holiday: str) -> None:
    db = next(get_db())
    try:
        db.query(MarketHoliday).filter_by(holiday=holiday).delete()
        db.commit()
    finally:
        db.close()
    load_market_hours_config()
