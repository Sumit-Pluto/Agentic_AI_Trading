#!/usr/bin/env python3
"""Phase-1 live acceptance: build a REAL chain via the Gateway, compute greeks,
and run one PAPER order end-to-end into the journal.

Proves the engine spine against a live (or cached) Shoonya session. Run on the
VPS with the Gateway up and connected. No REAL order is placed — the fill is
simulated by PaperBroker; only the journal is written.

    GATEWAY_BASE_URL=http://127.0.0.1:8000 \
    GATEWAY_CLIENT_ID=... GATEWAY_CLIENT_SECRET=... \
    python -m intraday.scripts.demo_paper --symbol NIFTY
"""
from __future__ import annotations

import argparse
import sys

from intraday import config as cfg_mod
from intraday.brokers import PaperBroker
from intraday.contracts import OrderIntent, Position, Signal
from intraday.data.context import LiveContext
from intraday.gateway_client import GatewayClient, GatewayError
from intraday.journal.store import Store
from intraday.options import greeks_for, ist_now


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    args = ap.parse_args()
    cfg = cfg_mod.load()

    try:
        client = GatewayClient()
    except GatewayError as e:
        print(f"[FAIL] {e}")
        return 2

    ctx = LiveContext(client, cfg)
    try:
        ch = ctx.chain(args.symbol)
    except GatewayError as e:
        print(f"[FAIL] could not build chain: {e}")
        return 2

    leg = ch.get(ch.atm, True)
    if leg is None or not leg.executable(1):
        print(f"[FAIL] ATM call not quotable (spot={ch.spot}, atm={ch.atm})")
        return 1
    g = greeks_for(leg, ch)
    print(f"spot={ch.spot} atm={ch.atm} expiry={ch.expiry} carry={ch.carry_rate}")
    print(f"ATM CE {leg.tsym}: bid={leg.bid} ask={leg.ask} iv={leg.iv} "
          f"delta={g['delta']:.3f} theta/day={g['theta']:.2f}")

    store = Store()
    now = ist_now()
    sig = Signal(ts=now, symbol=args.symbol, direction="BUY", score_buy=70,
                 score_sell=10, family_scores={}, agent_rows=[], vetoes=[],
                 n_scored=0, regime={"on": True, "scalar": 1.0},
                 instrument={"tsym": leg.tsym, "strike": leg.strike, "right": "CE"})
    sid = store.save_signal(sig)

    intent = OrderIntent(symbol=leg.tsym, side="BUY", qty=ch.lot_size,
                         order_type="MARKETABLE_LIMIT", limit_px=leg.ask,
                         reason="demo:paper", signal_id=sid, exch="NFO",
                         token=leg.token, strike=leg.strike, right="CE",
                         expiry=ch.expiry, lot_size=ch.lot_size, strategy="demo")
    broker = PaperBroker(slippage_pct=cfg.get("slippage_pct", 0.10))
    ack = broker.place(intent)
    oid = store.save_order(intent, status=ack["status"], broker=broker.name,
                           date=now.date(), broker_order_id=ack["broker_order_id"])
    if ack["status"] == "FILLED":
        store.fill_order(oid, ack["fill_px"])
        pos = Position(symbol=leg.tsym, qty=ch.lot_size, side="BUY",
                       entry_px=ack["fill_px"], entry_ts=now,
                       stop=ch.spot - 30, risk_per_share=max(leg.ask - leg.bid, 1.0),
                       strike=leg.strike, right="CE", expiry=ch.expiry,
                       lot_size=ch.lot_size, exch="NFO", token=leg.token,
                       underlying=args.symbol, entry_spot=ch.spot,
                       delta_at_entry=g["delta"], strategy="demo")
        store.open_position(pos)
        print(f"[PASS] paper FILLED @ {ack['fill_px']} — journal rows written "
              f"(signal={sid}, order={oid}); {len(store.open_positions())} open")
        store.close()
        return 0
    print(f"[FAIL] paper order not filled: {ack}")
    store.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
