# DB Models

SQLite database via SQLAlchemy 2.x. File: `gateway.db` (git-ignored).

---

## brokers

Seed registry — one row per broker type. Inserted on startup, never modified at runtime.

| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK | |
| name | STRING | UNIQUE, NOT NULL | `"shoonya"` or `"sharekhan"` |
| created_at | DATETIME | | Auto-set on insert |

**Seeded rows:** `shoonya`, `sharekhan`

---

## accounts

One row per user-configured broker account. Credentials are stored encrypted.

| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK | |
| broker_id | INTEGER | FK → brokers.id, NOT NULL | Which broker this account uses |
| label | STRING | NOT NULL | User-friendly name, e.g. `"Shoonya Main"` |
| credentials_enc | TEXT | NOT NULL | Fernet-encrypted JSON blob (see below) |
| is_active | BOOLEAN | default False | True while a live session exists |
| created_at | DATETIME | | Auto-set on insert |
| updated_at | DATETIME | | Auto-updated on save |

**credentials_enc** decrypts to a JSON object whose shape depends on the broker:

Shoonya:
```json
{
  "user_id": "...",
  "password": "...",
  "totp_secret": "...",
  "vendor_code": "...",
  "api_key": "...",
  "api_secret": "...",
  "imei": "...",
  "api_host": "https://api.shoonya.com/NorenWClientAPI"
}
```

Sharekhan *(future)*:
```json
{
  "client_id": "...",
  "password": "...",
  "totp_secret": "..."
}
```

---

## sessions

One active session per account. Upserted on connect, deleted on disconnect. Replaces `token_store.json`.

| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK | |
| account_id | INTEGER | FK → accounts.id, UNIQUE, NOT NULL | One session per account |
| token | TEXT | NOT NULL | Broker session token (`susertoken` for Shoonya) |
| broker_uid | STRING | | UID returned by broker on login |
| issued_at | DATETIME | NOT NULL | When the token was obtained |
| created_at | DATETIME | | Auto-set on insert |

---

## strategy_nodes

Registered strategy engine instances. Not currently seeded — no strategy
engines are registered by default (rows must be added manually if/when a
real strategy service, e.g. `snowball_engine`, needs to be tracked here).

| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK | |
| strategy_id | STRING | UNIQUE, NOT NULL | e.g. `"snowball_engine"` |
| display_name | STRING | NOT NULL | e.g. `"Snowball Engine"` |
| url | STRING | NOT NULL | Engine base URL |
| created_at | DATETIME | | Auto-set on insert |

---

## strategy_runs

One row per account per strategy invocation. `stopped_at = NULL` means currently running.

| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK | |
| strategy_node_id | INTEGER | FK → strategy_nodes.id, NOT NULL | Which engine |
| account_id | INTEGER | FK → accounts.id, NOT NULL | Which account |
| started_at | DATETIME | | Auto-set on insert |
| stopped_at | DATETIME | nullable | Set on stop; NULL = still running |

**Query for active runs:**
```python
db.query(StrategyRun).filter_by(
    strategy_node_id=node_id,
    account_id=account_id,
    stopped_at=None
).first()
```

---

## Relationships

```
brokers (1)
  └──< accounts (many)       broker_id FK
          │
          ├── sessions (1)   account_id FK, UNIQUE
          │
          └──< strategy_runs (many)   account_id FK
                    │
                    └── strategy_nodes (1)   strategy_node_id FK
```

| Relationship | Type | Rule |
|---|---|---|
| accounts → brokers | many-to-one | Each account belongs to exactly one broker |
| sessions → accounts | one-to-one | At most one live session per account |
| strategy_runs → accounts | many-to-one | Account can have many runs over time |
| strategy_runs → strategy_nodes | many-to-one | Engine can run on many accounts |
