from pydantic import BaseModel


class WatchlistItemResponse(BaseModel):
    tsym: str
    exch: str
    token: str
    lotsize: str
    instrumenttype: str
    expd: str
    sym: str

    class Config:
        from_attributes = True


class WatchlistItemCreate(BaseModel):
    tsym: str
    exch: str
    token: str
    lotsize: str = "1"
    instrumenttype: str = ""
    expd: str = ""
    sym: str = ""
