"""Register (or rotate) a machine/service identity that can authenticate to the
Gateway with client_id + client_secret and receive a scoped service JWT.

The raw client_secret is shown ONCE and is unrecoverable afterwards (only its
bcrypt hash is stored) — copy it into the service's .env immediately.

Usage (from gateway_backend/):
    python -m scripts.register_service --name snowball
    python -m scripts.register_service --name snowball --scopes orders,positions,funds,market,profile,status
    python -m scripts.register_service --name snowball --rotate   # new secret, keep the row/scopes
"""
import argparse
import secrets
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv

load_dotenv()  # JWT_SECRET must be present before importing security helpers

from app.core.security import ALL_SCOPES, hash_password  # noqa: E402
from db.engine import SessionLocal, init_db  # noqa: E402
from db.models import Service  # noqa: E402

# Default grant for snowball_engine — everything it calls, nothing more.
DEFAULT_SCOPES = "orders,positions,funds,market,profile,status"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help='service name, e.g. "snowball"')
    ap.add_argument("--scopes", default=DEFAULT_SCOPES,
                    help=f"comma-separated; valid: {','.join(ALL_SCOPES)}")
    ap.add_argument("--rotate", action="store_true",
                    help="regenerate the secret for an existing service (keeps scopes unless --scopes given)")
    args = ap.parse_args()

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    bad = [s for s in scopes if s not in ALL_SCOPES]
    if bad:
        ap.error(f"unknown scope(s): {', '.join(bad)}. Valid: {', '.join(ALL_SCOPES)}")

    init_db()  # ensure the `services` table exists
    db = SessionLocal()
    try:
        client_secret = secrets.token_urlsafe(32)
        service = db.query(Service).filter_by(name=args.name).first()

        if service is None:
            client_id = f"svc_{secrets.token_urlsafe(12)}"
            service = Service(
                name=args.name,
                client_id=client_id,
                client_secret_hash=hash_password(client_secret),
                scopes=",".join(scopes),
                is_active=True,
            )
            db.add(service)
            action = "created"
        else:
            if not args.rotate:
                ap.error(f"service '{args.name}' already exists. Use --rotate to issue a new secret.")
            service.client_secret_hash = hash_password(client_secret)
            if args.scopes != DEFAULT_SCOPES or not service.scopes:
                service.scopes = ",".join(scopes)
            service.is_active = True
            action = "rotated"

        db.commit()
        db.refresh(service)
    finally:
        db.close()

    print(f"\nService '{service.name}' {action}. Add these to the service's .env:\n")
    print(f"  GATEWAY_CLIENT_ID={service.client_id}")
    print(f"  GATEWAY_CLIENT_SECRET={client_secret}")
    print(f"\n  (scopes granted: {service.scopes})")
    print("\n⚠  The secret is shown only once — it cannot be recovered later.\n")


if __name__ == "__main__":
    main()
