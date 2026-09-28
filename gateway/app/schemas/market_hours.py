from pydantic import BaseModel, field_validator


def _validate_hhmm(v: str) -> str:
    """Accept and normalize an "HH:MM" 24h IST time string."""
    parts = (v or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError("time must be HH:MM")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("time must be HH:MM")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("time out of range")
    return f"{h:02d}:{m:02d}"


class ExchangeHours(BaseModel):
    exch: str
    open: str            # effective "HH:MM" (override if set, else default)
    close: str
    default_open: str    # the baked-in default, for the "reset" affordance
    default_close: str
    is_custom: bool      # True when a DB override is in force


class HolidayItem(BaseModel):
    date: str            # "YYYY-MM-DD"
    label: str = ""


class MarketHoursSettings(BaseModel):
    enforced: bool
    exchanges: list[ExchangeHours]
    holidays: list[HolidayItem]


class ExchangeHoursUpdate(BaseModel):
    open: str
    close: str

    _v_open = field_validator("open")(_validate_hhmm)
    _v_close = field_validator("close")(_validate_hhmm)


class HolidayCreate(BaseModel):
    date: str
    label: str = ""

    @field_validator("date")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        from datetime import date as _date
        try:
            _date.fromisoformat((v or "").strip())
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD")
        return v.strip()


class EnforcedUpdate(BaseModel):
    enforced: bool
