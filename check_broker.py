#!/usr/bin/env python3
"""Am I able to send buy/sell intraday orders to Shoonya right now?

    python check_broker.py               # read-only diagnostics
    python check_broker.py --order-test  # + REAL 1-share unfillable limit
                                         #   order, cancelled immediately

Read-only checks never place anything. --order-test submits ONE real BUY
LMT order for 1 share of YESBANK priced 20% below LTP (cannot fill) and
cancels it — any structured OMS response (accept+cancel, or a "market
closed"-style rejection) proves the order pipeline works end to end.
"""

import argparse
import logging
import sys

from dotenv import load_dotenv

from core.broker_check import run_checks, order_path_test
from shoonya_client import build_session


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--order-test", action="store_true",
                    help="place + cancel a real unfillable 1-share test order")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)-7s %(name)s: %(message)s")
    load_dotenv()

    session = build_session()
    restored = False
    try:
        restored = bool(session.restore_session())
    except Exception:
        pass
    if not restored:
        print("✗ no cached session for today (.session_token) — run the "
              "daily login first (python main.py / python -m shoonya_login)")
        return 1

    hub = None
    try:
        from quant.datahub import DataHub
        hub = DataHub(session)
    except Exception as e:
        print(f"  (scrip master / hub unavailable: {e})")

    print("\n── read-only broker checks ──────────────────────────────")
    checks = run_checks(session, hub)
    for c in checks:
        print(f"  {'✓' if c['ok'] else '✗'} {c['name']:<11} {c['detail']}")
    ok = all(c["ok"] for c in checks if c["name"] != "feed")

    if args.order_test:
        if not ok:
            print("\n✗ skipping --order-test: read-only checks failed")
            return 1
        print("\n── order-path test (REAL request, unfillable, cancelled) ─")
        res = order_path_test(session, hub)
        for s in res["steps"]:
            print(f"  {'✓' if s['ok'] else '✗'} {s['name']:<11} {s['detail']}")
        print(f"\n{'✓ ORDER PIPELINE PROVEN' if res['proved'] else '✗ ORDER PIPELINE NOT PROVEN'}"
              " — buy/sell requests "
              f"{'reach Shoonya OMS' if res['proved'] else 'do NOT reach Shoonya'}")
        return 0 if res["proved"] else 1

    print(f"\n{'✓ broker reachable — ready to send orders' if ok else '✗ broker NOT ready'}"
          "  (add --order-test to prove the order path with a real "
          "unfillable request)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
