"""shoonya_login/errors.py — exceptions raised by the Shoonya login flow."""
from __future__ import annotations


class ShoonyaError(RuntimeError):
    """Raised when the Shoonya API answers stat != Ok (or auth is missing)."""


class LoginRejectedError(ShoonyaError):
    """Raised when the broker rejects credentials outright (wrong password or
    wrong TOTP), as opposed to a transient browser/network hiccup.

    Carries the broker's own error text in `.reason` so callers can log/show
    exactly why, and so the caller knows NOT to retry — resubmitting a wrong
    password risks tripping the broker's own account lockout.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
