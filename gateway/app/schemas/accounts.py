from pydantic import BaseModel


class AccountCreate(BaseModel):
    broker_name: str
    label: str
    credentials: dict  # Will be encrypted before storage


class AccountResponse(BaseModel):
    id: int
    broker_name: str
    label: str
    is_active: bool

    class Config:
        from_attributes = True


class SessionTokenResponse(BaseModel):
    token: str
    broker_uid: str
    issued_at: str
    broker_name: str


class UserProfileResponse(BaseModel):
    broker_uid: str
    account_id: str
    email: str
    mobile: str
    broker_name: str
    full_name: str
    branch_id: str
    enabled_exchanges: list[str]
    enabled_products: list[str]
