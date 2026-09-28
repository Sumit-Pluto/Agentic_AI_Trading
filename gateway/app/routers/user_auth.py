"""App user authentication: signup / signin / me (JWT).

Distinct from `legacy_auth.py`, which handles the *broker* connection.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.schemas import AuthResponse, SigninRequest, SignupRequest, UserResponse
from db.engine import get_db
from db.models import User

router = APIRouter()


@router.post("/api/auth/signup", response_model=AuthResponse)
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(email=email, password_hash=hash_password(req.password), form_filled=False)
    db.add(user)
    db.commit()
    db.refresh(user)

    return AuthResponse(token=create_access_token(user.id), user=UserResponse.model_validate(user))


@router.post("/api/auth/signin", response_model=AuthResponse)
def signin(req: SigninRequest, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    user = db.query(User).filter_by(email=email).first()
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return AuthResponse(token=create_access_token(user.id), user=UserResponse.model_validate(user))


@router.get("/api/auth/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)):
    return UserResponse.model_validate(user)
