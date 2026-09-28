from fastapi import APIRouter, HTTPException

from app.core.deps import _require_legacy_auth
from app.schemas import OrderMarginRequest
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster

router = APIRouter()


@router.post("/api/order_margin")
async def order_margin(req: OrderMarginRequest):
    """SPAN+exposure margin required to place `req` — used by the engine as a
    pre-trade funds gate. Builds the Shoonya span_calculator position from the
    scripmaster metadata for the tradingsymbol."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    sm = get_scripmaster()
    scrip = sm.master.get(f"{req.exchange}|{req.tradingsymbol}", {}) if hasattr(sm, "master") else {}
    if not scrip:
        raise HTTPException(status_code=404, detail=f"{req.tradingsymbol} not in scripmaster")
    pos = {
        "prd": req.product_type or "M",
        "exch": req.exchange,
        "instname": scrip.get("instrumenttype", "") or "",
        "symname": scrip.get("sym", "") or "",
        "exd": scrip.get("expd", "") or "",
        "optt": scrip.get("opttype", "") or "",
        "strprc": str(scrip.get("strikeprice", "0") or "0"),
        "buyqty": str(req.quantity) if req.buy_or_sell == "B" else "0",
        "sellqty": str(req.quantity) if req.buy_or_sell == "S" else "0",
        "netqty": "0",
    }
    try:
        raw = await broker.getOrderMargin(token, credentials, [pos])
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="order margin not supported for this broker")
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya span error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"order margin error: {e}")

    def _f(key: str) -> float:
        try:
            return float(raw.get(key) or 0)
        except (ValueError, TypeError):
            return 0.0

    span = _f("span") or _f("span_trade")
    expo = _f("expo") or _f("expo_trade")
    required = round(span + expo, 2)
    return {"required": required, "span": span, "expo": expo,
            "stat": raw.get("stat", ""), "raw": raw}
