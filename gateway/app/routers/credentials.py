"""Per-user broker credential form.

A user configures exactly ONE broker (Shoonya or Sharekhan). Submitting the form
Fernet-encrypts the credentials into the user's single `Account` row and flips
`user.form_filled` to True. Re-submitting (even switching broker) replaces it.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import get_current_user, verify_password
from app.schemas import (
    ConfiguredBrokerResponse,
    CredentialRevealRequest,
    CredentialRevealResponse,
    CredentialsSubmit,
)
from crypto import decrypt_json, encrypt_json
from db.engine import get_db
from db.models import Account, Broker, User

router = APIRouter()

# Required credential keys per broker (must be present and non-empty).
# Field names match what the broker adapters / auth-code helpers read.
REQUIRED_FIELDS: dict[str, list[str]] = {
    "shoonya": ["user_id", "password", "totp_secret", "vendor_code", "api_key", "api_secret", "imei"],
    "sharekhan": ["user_id", "api_key", "secret_key", "customer_id", "password", "totp_secret"],
}


@router.get("/api/credentials", response_model=ConfiguredBrokerResponse | None)
def get_credentials(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the user's single configured broker (field NAMES only, never values)."""
    account = db.query(Account).filter_by(user_id=user.id).first()
    if account is None:
        return None
    return ConfiguredBrokerResponse(
        broker_name=account.broker.name,
        fields_present=REQUIRED_FIELDS.get(account.broker.name, []),
    )


@router.post("/api/credentials/reveal", response_model=CredentialRevealResponse)
def reveal_credentials(
    req: CredentialRevealRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's DECRYPTED credential values, gated by re-entering the app password.

    Password-in-body POST (never a query param) so the secret stays out of URLs/logs.
    """
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect password")

    account = db.query(Account).filter_by(user_id=user.id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="No broker credentials configured")

    try:
        credentials = decrypt_json(account.credentials_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored credentials")

    return CredentialRevealResponse(
        broker_name=account.broker.name,
        credentials=credentials,
    )


@router.post("/api/credentials", response_model=ConfiguredBrokerResponse)
def save_credentials(
    req: CredentialsSubmit,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    broker_name = req.broker_name.strip().lower()
    if broker_name not in REQUIRED_FIELDS:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {req.broker_name}")

    broker = db.query(Broker).filter_by(name=broker_name).first()
    if broker is None:
        raise HTTPException(status_code=400, detail=f"Unknown broker: {broker_name}")

    # Validate required fields present and non-empty.
    missing = [
        f for f in REQUIRED_FIELDS[broker_name]
        if not str(req.credentials.get(f, "")).strip()
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required {broker_name} fields: {', '.join(missing)}",
        )

    try:
        credentials_enc = encrypt_json(req.credentials)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Encryption failed: {e}")

    # One account per user: update in place if one exists, else create.
    account = db.query(Account).filter_by(user_id=user.id).first()
    if account is None:
        account = Account(
            user_id=user.id,
            broker_id=broker.id,
            label=f"{user.email} · {broker_name}",
            credentials_enc=credentials_enc,
            is_active=False,
        )
        db.add(account)
    else:
        account.broker_id = broker.id
        account.credentials_enc = credentials_enc
        account.updated_at = datetime.utcnow()

    user.form_filled = True
    db.commit()

    return ConfiguredBrokerResponse(
        broker_name=broker_name,
        fields_present=REQUIRED_FIELDS[broker_name],
    )
