from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from db.engine import Base


class User(Base):
    """An application user (JWT signup/signin).

    Distinct from the broker-level `Account`: a User authenticates to *this* app,
    and owns exactly one broker `Account` (created via the post-signup credential
    form). `form_filled` gates access — 0 until the user saves broker credentials.
    """
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True)
    email         = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)           # bcrypt hash
    form_filled   = Column(Boolean, default=False, nullable=False)  # False until broker creds saved
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    accounts = relationship("Account", back_populates="user")


class Service(Base):
    """A machine/service identity (e.g. snowball_engine) that authenticates to the
    Gateway with a client_id + client_secret and receives a scoped *service* JWT.

    Distinct from `User` (a human): services are non-interactive engines. What a
    service may do is limited by `scopes` (comma-separated, e.g. "orders,market"),
    enforced per-route by `require_scope` in app/core/security.py.
    """
    __tablename__ = "services"

    id                 = Column(Integer, primary_key=True)
    name               = Column(String, unique=True, nullable=False)   # "snowball"
    client_id          = Column(String, unique=True, nullable=False, index=True)
    client_secret_hash = Column(Text, nullable=False)                  # bcrypt hash of the client secret
    scopes             = Column(Text, nullable=False, default="")      # comma-separated grants
    is_active          = Column(Boolean, default=True, nullable=False)
    created_at         = Column(DateTime, default=datetime.utcnow)


class Broker(Base):
    __tablename__ = "brokers"

    id         = Column(Integer, primary_key=True)
    name       = Column(String, unique=True, nullable=False)  # "shoonya", "sharekhan"
    created_at = Column(DateTime, default=datetime.utcnow)

    accounts = relationship("Account", back_populates="broker")


class Account(Base):
    __tablename__ = "accounts"

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # owning app user (nullable: legacy rows predate users)
    broker_id       = Column(Integer, ForeignKey("brokers.id"), nullable=False)
    label           = Column(String, nullable=False)
    credentials_enc = Column(Text, nullable=False)           # Fernet-encrypted JSON
    is_active       = Column(Boolean, default=False)         # True while session is live
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user    = relationship("User", back_populates="accounts")
    broker  = relationship("Broker", back_populates="accounts")
    session = relationship("Session", back_populates="account", uselist=False)
    runs    = relationship("StrategyRun", back_populates="account")


class Session(Base):
    __tablename__ = "sessions"

    id         = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True, nullable=False)
    token      = Column(Text, nullable=False)       # susertoken string
    broker_uid = Column(String)                     # uid returned by broker on login
    issued_at  = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="session")


class StrategyNode(Base):
    __tablename__ = "strategy_nodes"

    id          = Column(Integer, primary_key=True)
    strategy_id = Column(String, unique=True, nullable=False)  # "future_rollover"
    display_name = Column(String, nullable=False)              # "Future Rollover"
    url         = Column(String, nullable=False)               # from env var
    created_at  = Column(DateTime, default=datetime.utcnow)

    runs = relationship("StrategyRun", back_populates="strategy_node")


class SymbolTarget(Base):
    """Per-symbol total-P&L auto-exit target.

    When the symbol's total P&L (realized + unrealized, valued at best bid for
    longs / best ask for shorts) hits target_value, all of that symbol's open
    positions are auto-exited. Flat threshold — not scaled by lot count.
    """
    __tablename__ = "symbol_targets"
    __table_args__ = (UniqueConstraint("exch", "tsym", name="uq_symbol_target"),)

    id           = Column(Integer, primary_key=True)
    exch         = Column(String, nullable=False)
    tsym         = Column(String, nullable=False)
    enabled      = Column(Boolean, default=False, nullable=False)
    target_value = Column(Float, default=10000.0, nullable=False)
    carried_pnl  = Column(Float, default=0.0, nullable=False)  # rollover P&L counted toward the target (in addition to this contract's live P&L)


class ExchangeTarget(Base):
    """Per-exchange total-P&L auto-exit target.

    Each exchange (NFO, MCX, NSE, …) gets its own cap because their session
    timings differ — a single all-exchange global would force one to inherit
    another's close. When an exchange's total P&L (realized + unrealized across
    its open positions) hits target_value, only that exchange's positions exit.
    """
    __tablename__ = "exchange_targets"
    __table_args__ = (UniqueConstraint("exch", name="uq_exchange_target"),)

    id           = Column(Integer, primary_key=True)
    exch         = Column(String, nullable=False)
    enabled      = Column(Boolean, default=False, nullable=False)
    target_value = Column(Float, default=50000.0, nullable=False)


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id               = Column(Integer, primary_key=True)
    strategy_node_id = Column(Integer, ForeignKey("strategy_nodes.id"), nullable=False)
    account_id       = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    started_at       = Column(DateTime, default=datetime.utcnow)
    stopped_at       = Column(DateTime, nullable=True)  # NULL = currently running

    strategy_node = relationship("StrategyNode", back_populates="runs")
    account       = relationship("Account", back_populates="runs")


class OrderLot(Base):
    __tablename__ = "order_lots"

    id                  = Column(Integer, primary_key=True)
    owner_uid           = Column(String, nullable=False, index=True)  # broker user_id (legacy auth)
    exch                = Column(String, nullable=False)
    tsym                = Column(String, nullable=False)
    token               = Column(String, default="")
    lotsize             = Column(Integer, default=1)
    product_type        = Column(String, default="M")
    side                = Column(String, nullable=False)               # "B" (long) or "S" (short)
    entry_qty           = Column(Integer, nullable=False)
    open_qty            = Column(Integer, nullable=False)
    avg_entry_price     = Column(Float, default=0.0)
    realized_pnl        = Column(Float, default=0.0)
    carried_pnl         = Column(Float, default=0.0, nullable=False)   # P&L rolled in from a prior contract (rollover); shown in live P&L, not this lot's own realized
    status              = Column(String, default="PENDING")            # PENDING|OPEN|PARTIAL|CLOSED|CANCELLED
    broker_entry_orderid= Column(String, default="")
    client_ref          = Column(String, unique=True, nullable=False)
    last_upldprc        = Column(Float, default=0.0)
    opened_at           = Column(DateTime, default=datetime.utcnow)
    closed_at           = Column(DateTime, nullable=True)
    target_enabled      = Column(Boolean, default=False, nullable=False)
    target_value        = Column(Float, default=0.0, nullable=False)  # live P&L threshold in ₹
    is_external         = Column(Boolean, default=False, nullable=False)
    source_service      = Column(String, nullable=True)   # name of the Service that placed this lot (e.g. "snowball" → SB badge); NULL = placed by a human in the Gateway UI
    is_rollover         = Column(Boolean, default=False, nullable=False)   # far leg auto-placed by a futures rollover (ROLL badge)
    is_reentry          = Column(Boolean, default=False, nullable=False)   # placed via the Re-entry button (RE badge)
    is_temp_exit        = Column(Boolean, default=False, nullable=False)   # TE tag: keep this CLOSED lot visible across days for re-entry/roll; cleared on re-entry
    reentry_source_lot_id = Column(Integer, nullable=True)   # prior lot this re-entry carried realized+carried P&L forward from
    persistent_order_id = Column(Integer, ForeignKey("persistent_orders.id"), nullable=True, index=True)
    description         = Column(Text, default="", nullable=False)

    exits = relationship("LotExit", back_populates="lot", cascade="all, delete-orphan")
    persistent_order = relationship(
        "PersistentOrder",
        back_populates="lots",
        foreign_keys=[persistent_order_id],
    )


class LotExit(Base):
    __tablename__ = "lot_exits"

    id                  = Column(Integer, primary_key=True)
    lot_id              = Column(Integer, ForeignKey("order_lots.id"), nullable=False, index=True)
    exit_qty            = Column(Integer, nullable=False)
    filled_qty          = Column(Integer, default=0)
    avg_exit_price      = Column(Float, default=0.0)
    broker_exit_orderid = Column(String, default="")
    client_ref          = Column(String, unique=True, nullable=False)
    status              = Column(String, default="PENDING")            # PENDING|FILLED|CANCELLED|REJECTED
    created_at          = Column(DateTime, default=datetime.utcnow)
    filled_at           = Column(DateTime, nullable=True)

    lot = relationship("OrderLot", back_populates="exits")


class RolloverIntent(Base):
    """A pending futures rollover: 'when this near exit fills, open the far
    contract automatically.'

    Created when the user clicks Roll (the near exit order is placed first).
    Consumed by the reconciliation path (_fire_ready_rollovers) the moment the
    near LotExit reaches FILLED — however long that takes — which then places
    the far entry for the actually-filled quantity and carries the target.
    """
    __tablename__ = "rollover_intents"

    id                  = Column(Integer, primary_key=True)
    owner_uid           = Column(String, nullable=False, index=True)
    lot_id              = Column(Integer, ForeignKey("order_lots.id"), nullable=False)
    exit_id             = Column(Integer, ForeignKey("lot_exits.id"), nullable=False, index=True)
    exch                = Column(String, nullable=False)
    near_tsym           = Column(String, nullable=False)
    far_tsym            = Column(String, nullable=False)
    far_token           = Column(String, default="")
    side                = Column(String, nullable=False)   # far entry side (= near lot side)
    product_type        = Column(String, default="M")
    price_type          = Column(String, default="LMT")    # LMT | MKT
    entry_price         = Column(Float, default=0.0)       # far LMT price; 0 → cached ask/bid at fire time
    carry_target        = Column(Boolean, default=True, nullable=False)
    carry_pnl           = Column(Boolean, default=True, nullable=False)  # seed the far lot's carried_pnl with the near leg's realized result
    near_target_enabled = Column(Boolean, default=False, nullable=False)
    near_target_value   = Column(Float, default=0.0, nullable=False)
    full_roll           = Column(Boolean, default=True, nullable=False)  # near lot fully rolled → may disable near target
    status              = Column(String, default="PENDING")  # PENDING|FIRING|DONE|FAILED
    far_order_id        = Column(String, default="")
    far_lot_id          = Column(Integer, nullable=True)
    error               = Column(Text, default="")
    acknowledged        = Column(Boolean, default=False, nullable=False)  # user dismissed the failure banner
    created_at          = Column(DateTime, default=datetime.utcnow)
    fired_at            = Column(DateTime, nullable=True)


class PersistentOrder(Base):
    """A user's intent to hold an entry order alive across sessions.

    Each row represents "keep trying to enter this position until it either
    fills or I cancel." A background sweeper submits a fresh DAY order once
    per trading day. On fill, the resulting OrderLot inherits the row's
    target_enabled / target_value.
    """
    __tablename__ = "persistent_orders"

    id                   = Column(Integer, primary_key=True)
    owner_uid            = Column(String, nullable=False, index=True)
    exch                 = Column(String, nullable=False)
    tsym                 = Column(String, nullable=False)
    token                = Column(String, default="")
    lotsize              = Column(Integer, default=1)
    side                 = Column(String, nullable=False)   # "B" or "S"
    product_type         = Column(String, default="M")      # C, M, I, H
    price_type           = Column(String, default="LMT")    # LMT or MKT
    price                = Column(Float,  default=0.0)
    trigger_price        = Column(Float,  default=0.0)
    quantity             = Column(Integer, nullable=False)
    target_enabled       = Column(Boolean, default=False, nullable=False)
    target_value         = Column(Float,  default=0.0, nullable=False)
    status               = Column(String, default="ACTIVE")  # ACTIVE|FILLED|USER_CANCELLED|FAILED
    last_broker_orderid  = Column(String, default="")
    last_submitted_at    = Column(DateTime, nullable=True)
    filled_lot_id        = Column(Integer, ForeignKey("order_lots.id"), nullable=True)
    description          = Column(Text, default="", nullable=False)
    created_at           = Column(DateTime, default=datetime.utcnow)
    updated_at           = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lots = relationship("OrderLot", back_populates="persistent_order",
                        foreign_keys="OrderLot.persistent_order_id")


class MarketHours(Base):
    """Per-exchange trading-window override (IST).

    A row here overrides the baked-in default in `session_windows` for that
    exchange — e.g. pin MCX's evening close, or absorb a schedule change —
    without a code edit. Absent → the (DST-aware) default is used. Times are
    stored as "HH:MM" strings; the loader parses them into `datetime.time`.
    """
    __tablename__ = "market_hours"
    __table_args__ = (UniqueConstraint("exch", name="uq_market_hours_exch"),)

    id         = Column(Integer, primary_key=True)
    exch       = Column(String, nullable=False)
    open_time  = Column(String, nullable=False)   # "HH:MM" IST
    close_time = Column(String, nullable=False)   # "HH:MM" IST
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MarketHoliday(Base):
    """A user-added market holiday (IST date), additive to the baked-in national
    list in `session_windows` — lets the calendar roll into a new year from the
    Settings modal instead of a yearly code change."""
    __tablename__ = "market_holidays"
    __table_args__ = (UniqueConstraint("holiday", name="uq_market_holiday"),)

    id         = Column(Integer, primary_key=True)
    holiday    = Column(String, nullable=False)   # "YYYY-MM-DD" IST
    label      = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    """Tiny key→value store for global app settings (e.g. whether the
    market-hours order gate is enforced). Kept generic so future flags don't
    each need a table."""
    __tablename__ = "app_settings"

    key   = Column(String, primary_key=True)
    value = Column(String, nullable=False)


class OptionOISnapshot(Base):
    """Latest open interest seen for one option contract on one trading day.

    Shoonya's quote carries today's `oi` but no previous-day figure, and the
    daily candle series carries no OI column at all — so OI *change*, which is
    what distinguishes a strike being built from one being unwound, cannot be
    obtained from the broker in any single call. The gateway therefore records
    what it observes: every option-chain fetch upserts the day's latest OI, and
    the most recent EARLIER day's row is served back as `prev_oi`.

    Consequence worth stating plainly: this is empty until the gateway has seen
    two separate sessions, so a consumer must read prev_oi=0 as "unknown", never
    as "no change".
    """
    __tablename__ = "option_oi_snapshots"
    __table_args__ = (UniqueConstraint("exch", "token", "trade_date", name="uq_oi_snap"),)

    id         = Column(Integer, primary_key=True)
    exch       = Column(String(16), nullable=False)
    token      = Column(String(32), nullable=False)
    trade_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD, IST
    oi         = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"
    __table_args__ = (UniqueConstraint("exch", "token", name="uq_watchlist_exch_token"),)

    id             = Column(Integer, primary_key=True)
    tsym           = Column(String, nullable=False)
    exch           = Column(String, nullable=False)
    token          = Column(String, nullable=False)
    lotsize        = Column(String, default="1")
    instrumenttype = Column(String, default="")
    expd           = Column(String, default="")
    sym            = Column(String, default="")
    sort_order     = Column(Integer, default=0)
    created_at     = Column(DateTime, default=datetime.utcnow)
