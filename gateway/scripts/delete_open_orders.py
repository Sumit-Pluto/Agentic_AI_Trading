"""Delete all open OrderLot rows (status PENDING/OPEN/PARTIAL) from gateway.db.

Dry-run by default — lists what would be deleted. Pass --execute to commit.

Usage (from gateway_backend/):
    python -m scripts.delete_open_orders            # dry run
    python -m scripts.delete_open_orders --execute   # actually delete
"""
import argparse
import sys

sys.path.insert(0, ".")

from db.engine import SessionLocal
from db.models import OrderLot, LotExit

OPEN_STATUSES = ["PENDING", "OPEN", "PARTIAL"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually delete rows (default is dry-run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        lots = db.query(OrderLot).filter(OrderLot.status.in_(OPEN_STATUSES)).all()

        if not lots:
            print("No open orders found.")
            return

        print(f"Found {len(lots)} open order(s):")
        for lot in lots:
            print(f"  id={lot.id} status={lot.status} {lot.side} {lot.tsym} qty={lot.open_qty} client_ref={lot.client_ref}")

        lot_ids = [lot.id for lot in lots]
        exits = db.query(LotExit).filter(LotExit.lot_id.in_(lot_ids)).all()
        if exits:
            print(f"\nNote: {len(exits)} lot_exits row(s) reference these lots and will be deleted too (cascade).")

        if not args.execute:
            print("\nDry run only — no rows deleted. Re-run with --execute to delete.")
            return

        for lot in lots:
            db.delete(lot)  # cascade="all, delete-orphan" removes matching lot_exits too
        db.commit()
        print(f"\nDeleted {len(lots)} open order(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
