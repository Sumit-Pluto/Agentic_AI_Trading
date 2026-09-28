from fastapi import APIRouter, HTTPException

from app.schemas import (
    EnforcedUpdate,
    ExchangeHoursUpdate,
    HolidayCreate,
    MarketHoursSettings,
)
from app.services.market_hours import (
    add_holiday,
    get_market_hours_settings,
    remove_holiday,
    reset_exchange_hours,
    set_enforced_setting,
    upsert_exchange_hours,
)
from session_windows import known_exchanges

router = APIRouter()


@router.get("/api/market-hours", response_model=MarketHoursSettings)
async def get_market_hours():
    return get_market_hours_settings()


@router.put("/api/market-hours/enforced", response_model=MarketHoursSettings)
async def update_enforced(req: EnforcedUpdate):
    set_enforced_setting(req.enforced)
    return get_market_hours_settings()


@router.put("/api/market-hours/{exch}", response_model=MarketHoursSettings)
async def update_exchange_hours(exch: str, req: ExchangeHoursUpdate):
    exch = exch.upper()
    if exch not in known_exchanges():
        raise HTTPException(status_code=404, detail=f"Unknown exchange: {exch}")
    if req.open >= req.close:
        raise HTTPException(status_code=400, detail="Open time must be before close time")
    upsert_exchange_hours(exch, req.open, req.close)
    return get_market_hours_settings()


@router.delete("/api/market-hours/{exch}", response_model=MarketHoursSettings)
async def reset_exchange_hours_route(exch: str):
    reset_exchange_hours(exch.upper())
    return get_market_hours_settings()


@router.post("/api/market-hours/holidays", response_model=MarketHoursSettings)
async def create_holiday(req: HolidayCreate):
    add_holiday(req.date, req.label)
    return get_market_hours_settings()


@router.delete("/api/market-hours/holidays/{holiday}", response_model=MarketHoursSettings)
async def delete_holiday(holiday: str):
    remove_holiday(holiday)
    return get_market_hours_settings()
