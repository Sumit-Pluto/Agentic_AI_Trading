from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.schemas import WatchlistItemCreate, WatchlistItemResponse
from db.engine import get_db
from db.models import WatchlistEntry

router = APIRouter()


@router.get("/api/watchlist", response_model=list[WatchlistItemResponse])
def get_watchlist(db: Session = Depends(get_db)):
    return db.query(WatchlistEntry).order_by(WatchlistEntry.sort_order, WatchlistEntry.id).all()


@router.post("/api/watchlist", response_model=WatchlistItemResponse, status_code=201)
def add_watchlist_item(req: WatchlistItemCreate, db: Session = Depends(get_db)):
    existing = db.query(WatchlistEntry).filter_by(exch=req.exch, token=req.token).first()
    if existing:
        return existing
    max_order = db.query(WatchlistEntry).count()
    entry = WatchlistEntry(
        tsym=req.tsym,
        exch=req.exch,
        token=req.token,
        lotsize=req.lotsize,
        instrumenttype=req.instrumenttype,
        expd=req.expd,
        sym=req.sym,
        sort_order=max_order,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/api/watchlist/{exch}/{token}", status_code=204)
def remove_watchlist_item(exch: str, token: str, db: Session = Depends(get_db)):
    entry = db.query(WatchlistEntry).filter_by(exch=exch, token=token).first()
    if entry:
        db.delete(entry)
        db.commit()
