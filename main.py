#!/usr/bin/env python3
"""Demo: connect to Shoonya (2026 OAuth API), stream live ticks + order updates.

This is a connectivity smoke test, nothing more — it prints ticks for two
index symbols to your terminal and stops. It does NOT feed the dashboard or
the trading agents; those get their prices through a separate connection
opened by run_app.py. Login logic used to live in this file too; it now
lives in shoonya_login/ — see that package for the daily login flow, and
`python -m shoonya_login` for the unattended 8:45am cron login.

Setup (one time):
    source venv/bin/activate    # deps already installed here
    cp .env.example .env        # then fill YOUR credentials into .env

Daily login (until `python -m shoonya_login` runs on the whitelisted VPS):
    python main.py              # it prints the login URL and asks for the code

IMPORTANT (April-2026 API rules):
  * Log in FROM THE MACHINE whose static IP you registered in the Shoonya
    portal (profile → API Key → Primary IP) — other IPs get "invalid IP".
  * The auth code → access token is cached in .session_token for the day;
    restarts need no browser step.
"""

import logging
import queue
import sys
import time

from dotenv import load_dotenv

from shoonya_client import ShoonyaFeed, build_session
from shoonya_login import check_registered_ip, daily_login

# Nifty 50 and Nifty Bank index tokens — replace/add via load_scripmaster()
WATCH = ["NSE|26000", "NSE|26009"]


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    load_dotenv()
    session = build_session()
    check_registered_ip()
    daily_login(session)

    # Callbacks run on the socket thread → only enqueue, process here.
    ticks = queue.Queue()

    def on_tick(msg, snap):
        ticks.put((time.time(), msg, snap))

    def on_order(msg):
        logging.info("ORDER UPDATE: %s %s qty=%s fill=%s avg=%s status=%s %s",
                     msg.get("trantype"), msg.get("tsym"), msg.get("qty"),
                     msg.get("fillshares", "-"), msg.get("avgprc", "-"),
                     msg.get("status"), msg.get("rejreason", ""))

    feed = ShoonyaFeed(session, on_tick=on_tick, on_order=on_order)
    feed.start()
    if not feed.wait_connected(20):
        sys.exit("websocket did not authenticate within 20s — if the log "
                 "shows 'auth rejected', delete .session_token and rerun "
                 "(fresh daily login); also confirm you are on the "
                 "whitelisted-IP machine.")
    feed.subscribe_orders()
    feed.subscribe(WATCH)
    print(f"streaming {WATCH} — Ctrl-C to exit")

    try:
        while True:
            try:
                recv_t, msg, snap = ticks.get(timeout=1)
            except queue.Empty:
                continue
            lag = ""
            if msg.get("ft"):   # exchange feed time (1-second resolution)
                lag = f"  lag~{recv_t - int(msg['ft']):+.1f}s"
            print(f"{snap.get('e')}|{snap.get('tk')} {snap.get('ts', ''):<16} "
                  f"ltp={snap.get('lp'):<10} o={snap.get('o')} h={snap.get('h')} "
                  f"l={snap.get('l')} c={snap.get('c')}{lag}")
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop()
        print("stopped.")


if __name__ == "__main__":
    main()
