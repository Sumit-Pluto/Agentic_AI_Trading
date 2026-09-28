from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.schemas import (
    AccountCreate,
    AccountResponse,
    SessionTokenResponse,
    UserProfileResponse,
)
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from crypto import decrypt_json, encrypt_json
from db.engine import get_db
from db.models import Account, Broker, Session as SessionModel

router = APIRouter()


@router.get("/api/accounts")
def list_accounts(db: Session = Depends(get_db)):
    """Get all accounts for the user."""
    accounts = db.query(Account).all()
    return [
        AccountResponse(
            id=acc.id,
            broker_name=acc.broker.name,
            label=acc.label,
            is_active=acc.is_active,
        )
        for acc in accounts
    ]


@router.post("/api/accounts")
def create_account(req: AccountCreate, db: Session = Depends(get_db)):
    """Create a new account with encrypted credentials."""
    # Verify broker exists
    broker = db.query(Broker).filter_by(name=req.broker_name.lower()).first()
    if not broker:
        raise HTTPException(status_code=400, detail=f"Unknown broker: {req.broker_name}")

    # Encrypt credentials
    try:
        credentials_enc = encrypt_json(req.credentials)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Encryption failed: {e}")

    # Create account
    account = Account(
        broker_id=broker.id,
        label=req.label,
        credentials_enc=credentials_enc,
        is_active=False,
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    return AccountResponse(
        id=account.id,
        broker_name=broker.name,
        label=account.label,
        is_active=account.is_active,
    )


@router.post("/api/accounts/{account_id}/connect")
async def connect_account(account_id: int, db: Session = Depends(get_db)):
    """Authenticate with the broker for a specific account."""
    # Fetch account
    account = db.query(Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Decrypt credentials
    try:
        credentials = decrypt_json(account.credentials_enc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Decryption failed: {e}")

    # Get broker adapter
    try:
        broker = get_broker(account.broker.name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {account.broker.name}")

    # Authenticate
    try:
        token = await broker.login(credentials)
    except BrokerError as e:
        raise HTTPException(status_code=401, detail=f"Auth failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Authentication error: {e}")

    # Store session in DB
    existing_session = db.query(SessionModel).filter_by(account_id=account.id).first()
    if existing_session:
        db.delete(existing_session)

    session = SessionModel(
        account_id=account.id,
        token=token.token,
        broker_uid=token.broker_uid,
        issued_at=token.issued_at,
    )
    db.add(session)
    account.is_active = True
    account.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(session)

    return SessionTokenResponse(
        token=token.token,
        broker_uid=token.broker_uid,
        issued_at=token.issued_at,
        broker_name=account.broker.name,
    )


@router.post("/api/accounts/{account_id}/disconnect")
async def disconnect_account(account_id: int, db: Session = Depends(get_db)):
    """Disconnect a broker session for an account."""
    # Fetch account and session
    account = db.query(Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    session = db.query(SessionModel).filter_by(account_id=account.id).first()
    if not session:
        return {"disconnected": True, "message": "No active session"}

    # Decrypt credentials
    try:
        credentials = decrypt_json(account.credentials_enc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Decryption failed: {e}")

    # Get broker adapter and call logout
    try:
        broker = get_broker(account.broker.name)
        token = SessionToken(
            token=session.token,
            broker_uid=session.broker_uid,
            issued_at=session.issued_at,
            broker_name=account.broker.name,
        )
        await broker.logout(token, credentials)
    except Exception as e:
        # Log but don't fail if broker logout fails
        print(f"Broker logout error: {e}")

    # Delete session from DB
    db.delete(session)
    account.is_active = False
    account.updated_at = datetime.utcnow()
    db.commit()

    return {"disconnected": True}


@router.get("/api/accounts/{account_id}/user")
async def get_account_user(account_id: int, db: Session = Depends(get_db)):
    """Get user profile from broker for an account."""
    # Fetch account and session
    account = db.query(Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    session = db.query(SessionModel).filter_by(account_id=account.id).first()
    if not session:
        raise HTTPException(status_code=401, detail="Account not connected")

    # Decrypt credentials
    try:
        credentials = decrypt_json(account.credentials_enc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Decryption failed: {e}")

    # Get broker adapter and fetch user profile
    try:
        broker = get_broker(account.broker.name)
        token = SessionToken(
            token=session.token,
            broker_uid=session.broker_uid,
            issued_at=session.issued_at,
            broker_name=account.broker.name,
        )
        profile = await broker.get_user_profile(token, credentials)
        return UserProfileResponse(**{
            k: v for k, v in profile.__dict__.items()
            if not k.startswith("_")
        })
    except BrokerError as e:
        raise HTTPException(status_code=401, detail=f"Auth error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"User profile error: {e}")
