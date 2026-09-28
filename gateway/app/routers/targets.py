import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core import state
from app.schemas import (
    ExchangeTargetResponse,
    ExchangeTargetUpdate,
    SymbolTargetResponse,
    SymbolTargetUpdate,
)
from app.services.targets import _load_target_positions
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import ExchangeTarget, SymbolTarget

router = APIRouter()


@router.get("/api/symbol-targets", response_model=list[SymbolTargetResponse])
async def get_symbol_targets(db: Session = Depends(get_db)):
    return db.query(SymbolTarget).all()


@router.put("/api/symbol-targets/{exch}/{tsym}", response_model=SymbolTargetResponse)
async def update_symbol_target(exch: str, tsym: str, req: SymbolTargetUpdate, db: Session = Depends(get_db)):
    cfg = db.query(SymbolTarget).filter_by(exch=exch, tsym=tsym).first()
    if not cfg:
        cfg = SymbolTarget(exch=exch, tsym=tsym, enabled=False, target_value=10000.0)
        db.add(cfg)
    if req.enabled is not None:
        cfg.enabled = req.enabled
    if req.target_value is not None:
        cfg.target_value = req.target_value
    # carried_pnl: a rollover sets it explicitly; a fresh manual target_value
    # (with no carry given) resets it — "exit when THIS position makes X".
    if req.carried_pnl is not None:
        cfg.carried_pnl = req.carried_pnl
    elif req.target_value is not None:
        cfg.carried_pnl = 0.0
    # A ₹0 (or negative) target would fire the moment P&L touches breakeven.
    if cfg.enabled and (cfg.target_value or 0) <= 0:
        db.rollback()
        raise HTTPException(status_code=400, detail="Target value must be positive to enable")
    db.commit()

    sm = get_scripmaster()
    tok = sm.get_token(exch, tsym)
    if tok:
        key = f"{exch}|{tok}"
        state._symbol_targets[key] = {"enabled": cfg.enabled, "target_value": cfg.target_value, "carried_pnl": cfg.carried_pnl or 0.0}
        if cfg.enabled:
            # Re-arm (a prior session hit may have latched this key) and
            # refresh the cached positions so the tick loop can price it.
            state._auto_exited_tokens.discard(key)
            asyncio.create_task(_load_target_positions())

    return SymbolTargetResponse(exch=cfg.exch, tsym=cfg.tsym, enabled=cfg.enabled, target_value=cfg.target_value, carried_pnl=cfg.carried_pnl or 0.0)


@router.get("/api/exchange-targets", response_model=list[ExchangeTargetResponse])
async def get_exchange_targets(db: Session = Depends(get_db)):
    return db.query(ExchangeTarget).all()


@router.put("/api/exchange-targets/{exch}", response_model=ExchangeTargetResponse)
async def update_exchange_target(exch: str, req: ExchangeTargetUpdate, db: Session = Depends(get_db)):
    cfg = db.query(ExchangeTarget).filter_by(exch=exch).first()
    if not cfg:
        cfg = ExchangeTarget(exch=exch, enabled=False, target_value=50000.0)
        db.add(cfg)
    if req.enabled is not None:
        cfg.enabled = req.enabled
    if req.target_value is not None:
        cfg.target_value = req.target_value
    if cfg.enabled and (cfg.target_value or 0) <= 0:
        db.rollback()
        raise HTTPException(status_code=400, detail="Target value must be positive to enable")
    db.commit()
    state._exchange_targets[exch] = {"enabled": cfg.enabled, "target_value": cfg.target_value}
    if cfg.enabled:
        # Re-arm this exchange (a prior session hit may have latched it) and
        # refresh the cached positions so the tick loop can price the cap.
        state._exchange_target_exited.discard(exch)
        asyncio.create_task(_load_target_positions())
    return ExchangeTargetResponse(exch=cfg.exch, enabled=cfg.enabled, target_value=cfg.target_value)
