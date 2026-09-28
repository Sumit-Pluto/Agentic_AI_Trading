from fastapi import APIRouter, HTTPException

from app.core.deps import _require_legacy_auth
from app.schemas import FundsResponse
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker

router = APIRouter()


@router.get("/api/funds", response_model=FundsResponse)
async def get_funds():
    """Fetch account cash balance and margin utilisation from Shoonya."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    try:
        raw = await broker.getMargins(token, credentials)
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching funds: {e}")

    def _f(key: str) -> float:
        val = raw.get(key, "0") or "0"
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    return FundsResponse(
        cash=_f("cash"),
        margin_used=_f("marginused"),
        payin=_f("payin"),
        collateral=_f("brkcollamt"),
    )
