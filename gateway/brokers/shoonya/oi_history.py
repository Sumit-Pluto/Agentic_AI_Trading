"""Day-over-day open-interest for option contracts.

The broker gives no previous-day OI: the REST quote has only today's `oi`, and
the daily price series has no OI column at all. Without it, "fresh OI build at
the 1400 call" — the single most-used read of an option chain — is unavailable.

So the gateway keeps its own record. Each option-chain fetch upserts the latest
OI it saw per contract for today (`record`), and reads back the most recent
strictly-earlier day (`previous`). Writes are best-effort: a snapshot failure
must never fail the chain request that produced it, since the chain itself is
still correct without the OI delta.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import tuple_

from db.engine import SessionLocal
from db.models import OptionOISnapshot

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> str:
    """The broker's trading date. Deliberately IST and not the host's local
    date: the server runs UTC, so after 18:30 UTC a naive `date.today()` would
    roll to tomorrow mid-session and split one trading day across two rows."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def previous(exch: str, tokens: list[str]) -> dict[str, int]:
    """{token: OI as of the most recent earlier trading day}.

    Tokens with no prior snapshot are simply absent from the mapping — the
    caller reports 0/unknown rather than inventing a delta.
    """
    if not tokens:
        return {}
    day = today_ist()
    db = SessionLocal()
    try:
        rows = (db.query(OptionOISnapshot)
                  .filter(OptionOISnapshot.exch == exch,
                          OptionOISnapshot.token.in_(tokens),
                          OptionOISnapshot.trade_date < day)
                  .order_by(OptionOISnapshot.token,
                            OptionOISnapshot.trade_date.desc())
                  .all())
    except Exception as e:
        logger.warning("oi_history.previous failed: %s", e)
        return {}
    finally:
        db.close()

    # rows are token-grouped, newest-first within each token, so the first row
    # per token is that token's latest earlier day.
    out: dict[str, int] = {}
    for r in rows:
        out.setdefault(r.token, int(r.oi or 0))
    return out


def record(exch: str, oi_by_token: dict[str, int]) -> None:
    """Upsert today's OI for these contracts. Best-effort and never raises."""
    pairs = [(t, int(v)) for t, v in oi_by_token.items() if t and v and int(v) > 0]
    if not pairs:
        return
    day = today_ist()
    db = SessionLocal()
    try:
        existing = {
            r.token: r
            for r in db.query(OptionOISnapshot).filter(
                OptionOISnapshot.trade_date == day,
                tuple_(OptionOISnapshot.exch, OptionOISnapshot.token).in_(
                    [(exch, t) for t, _ in pairs]),
            ).all()
        }
        for token, oi in pairs:
            row = existing.get(token)
            if row is None:
                db.add(OptionOISnapshot(exch=exch, token=token,
                                        trade_date=day, oi=oi))
            elif row.oi != oi:
                row.oi = oi
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("oi_history.record failed for %s (%d contracts): %s",
                       exch, len(pairs), e)
    finally:
        db.close()
