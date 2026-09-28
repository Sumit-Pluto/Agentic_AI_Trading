"""Service (machine-to-machine) authentication.

An engine such as snowball_engine authenticates with a client_id + client_secret
(created via scripts/register_service.py) and receives a short-lived, scoped
*service* JWT. Distinct from `user_auth.py` (human login) and `legacy_auth.py`
(broker connection).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import (
    _SERVICE_TOKEN_TTL,
    create_service_token,
    verify_password,
)
from app.schemas import ServiceTokenRequest, ServiceTokenResponse
from db.engine import get_db
from db.models import Service

router = APIRouter()


@router.post("/api/auth/service-token", response_model=ServiceTokenResponse)
def issue_service_token(req: ServiceTokenRequest, db: Session = Depends(get_db)):
    """Client-credentials grant: exchange client_id + client_secret for a service JWT."""
    service = db.query(Service).filter_by(client_id=req.client_id, is_active=True).first()
    if service is None or not verify_password(req.client_secret, service.client_secret_hash):
        raise HTTPException(status_code=401, detail="Invalid client credentials")

    scopes = [s for s in (service.scopes or "").split(",") if s]
    return ServiceTokenResponse(
        token=create_service_token(service.client_id, scopes),
        scopes=scopes,
        expires_in=int(_SERVICE_TOKEN_TTL.total_seconds()),
    )
