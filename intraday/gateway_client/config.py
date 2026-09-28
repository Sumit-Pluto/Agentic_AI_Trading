"""Gateway connection settings for the intraday engine (env-driven)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class GatewaySettings:
    base_url: str = "http://127.0.0.1:8000"
    ws_url: str = "ws://127.0.0.1:8000/api/ws/ticker"
    client_id: str = ""
    client_secret: str = ""
    timeout: float = 20.0
    verify_tls: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @classmethod
    def load(cls) -> "GatewaySettings":
        base = os.getenv("GATEWAY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        ws = os.getenv("GATEWAY_WS_URL") or (base.replace("http", "ws", 1)
                                             + "/api/ws/ticker")
        try:
            timeout = float(os.getenv("GATEWAY_TIMEOUT", "20"))
        except ValueError as e:
            raise ValueError(f"GATEWAY_TIMEOUT must be a number of seconds: {e}")
        verify = (os.getenv("GATEWAY_VERIFY_TLS", "1").strip().lower()
                  not in ("0", "false", "no"))
        return cls(base_url=base, ws_url=ws,
                   client_id=os.getenv("GATEWAY_CLIENT_ID", ""),
                   client_secret=os.getenv("GATEWAY_CLIENT_SECRET", ""),
                   timeout=timeout, verify_tls=verify)
